import asyncio
import base64
import hashlib
import io
import json
import os
import shlex
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

import paramiko
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, root_validator, validator

RUN_TTL_SECONDS = 1800  # 30 minutes
MAX_CONCURRENT_RUNS = 10
MAX_LOG_LINES = 2000
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
SSH_CONNECT_TIMEOUT = 10
SSH_COMMAND_TIMEOUT = 120

app = FastAPI(title="WarOps Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuthPassword(BaseModel):
    type: str = Field("password", const=True)
    password: str

    @validator("password")
    def password_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("password must not be empty")
        return v


class AuthKey(BaseModel):
    type: str = Field("key", const=True)
    privateKey: str
    passphrase: Optional[str] = None

    @validator("privateKey")
    def key_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("privateKey must not be empty")
        return v


class ServerModel(BaseModel):
    host: str
    port: int = Field(..., ge=1, le=65535)
    username: str
    auth: Any
    expectedFingerprint: Optional[str] = None

    @validator("host")
    def host_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("host must not be empty")
        return v

    @validator("username")
    def username_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("username must not be empty")
        return v

    @root_validator(pre=True)
    def validate_auth(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        auth = values.get("auth")
        if not isinstance(auth, dict) or "type" not in auth:
            raise ValueError("auth.type is required")
        auth_type = auth.get("type")
        if auth_type == "password":
            values["auth"] = AuthPassword(**auth)
        elif auth_type == "key":
            values["auth"] = AuthKey(**auth)
        else:
            raise ValueError("auth.type must be 'password' or 'key'")
        return values


class TemplateSelection(BaseModel):
    id: str
    settings: Dict[str, Any] = Field(default_factory=dict)

    @validator("id")
    def id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("template id must not be empty")
        return v


class OfflineConfig(BaseModel):
    enabled: bool = False
    aptProxy: Optional[str] = None
    dockerRegistry: Optional[str] = None
    artifactMirror: Optional[str] = None


class RunRequest(BaseModel):
    server: ServerModel
    templates: List[TemplateSelection]
    offline: OfflineConfig = Field(default_factory=OfflineConfig)

    @validator("templates")
    def templates_non_empty(cls, v: List[TemplateSelection]) -> List[TemplateSelection]:
        if not v:
            raise ValueError("at least one template must be provided")
        return v


class SSHTestRequest(ServerModel):
    pass


@dataclass
class LogEntry:
    ts: datetime
    level: str
    message: str
    progress: Optional[int] = None
    step: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "ts": self.ts.isoformat(),
            "level": self.level,
            "message": self.message,
        }
        if self.progress is not None:
            data["progress"] = self.progress
        if self.step is not None:
            data["step"] = self.step
        return data


@dataclass
class RunRecord:
    id: str
    status: str = "queued"
    progress: int = 0
    current_step: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    logs: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    listeners: List[asyncio.Queue] = field(default_factory=list)

    def add_log(self, level: str, message: str, progress: Optional[int] = None, step: Optional[str] = None):
        entry = LogEntry(ts=datetime.utcnow(), level=level, message=message, progress=progress, step=step)
        self.logs.append(entry)
        for listener in list(self.listeners):
            try:
                listener.put_nowait(entry)
            except asyncio.QueueFull:
                continue

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_LOG_LINES)
        self.listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.listeners:
            self.listeners.remove(q)


class TemplateManifest(BaseModel):
    id: str
    name: str
    category: str
    description: str
    requires: List[str] = Field(default_factory=list)
    supportsOffline: bool = False
    fields: List[Dict[str, Any]] = Field(default_factory=list)
    outputs: List[Dict[str, Any]] = Field(default_factory=list)


class TemplateLoader:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def list_templates(self) -> List[TemplateManifest]:
        templates: List[TemplateManifest] = []
        for manifest_path in sorted(self.base_dir.glob("*/manifest.json")):
            with manifest_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            manifest = TemplateManifest(**data)
            templates.append(manifest)
        return templates

    def load(self, template_id: str) -> TemplateManifest:
        manifest_path = self.base_dir / template_id / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"template {template_id} not found")
        with manifest_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return TemplateManifest(**data)

    def load_steps(self, template_id: str) -> str:
        steps_path = self.base_dir / template_id / "steps.sh"
        if not steps_path.exists():
            raise FileNotFoundError(f"steps.sh for template {template_id} not found")
        return steps_path.read_text(encoding="utf-8")


class RunStore:
    def __init__(self):
        self.runs: Dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)

    async def create_run(self, record: RunRecord) -> None:
        async with self._lock:
            self.runs[record.id] = record

    async def get(self, run_id: str) -> RunRecord:
        async with self._lock:
            if run_id not in self.runs:
                raise KeyError("run not found")
            return self.runs[run_id]

    async def cleanup(self) -> None:
        now = datetime.utcnow()
        async with self._lock:
            to_delete = []
            for run_id, run in self.runs.items():
                if run.finished_at and (now - run.finished_at) > timedelta(seconds=RUN_TTL_SECONDS):
                    to_delete.append(run_id)
            for run_id in to_delete:
                del self.runs[run_id]

    async def acquire_slot(self) -> None:
        await self._semaphore.acquire()

    def release_slot(self) -> None:
        self._semaphore.release()


run_store = RunStore()
template_loader = TemplateLoader(TEMPLATES_DIR)


def mask_secret(text: str, secrets: List[str]) -> str:
    masked = text
    for secret in secrets:
        if secret:
            masked = masked.replace(secret, "***")
    return masked


def compute_fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode()


def load_private_key(private_key: str, passphrase: Optional[str]) -> paramiko.PKey:
    key_bytes = io.StringIO(private_key)
    loaders = [paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key]
    last_error: Optional[Exception] = None
    for loader in loaders:
        try:
            key_bytes.seek(0)
            return loader.from_private_key(key_bytes, password=passphrase)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise last_error or ValueError("Invalid private key")


def ssh_connect(server: ServerModel) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        if server.auth.type == "password":
            client.connect(
                hostname=server.host,
                port=server.port,
                username=server.username,
                password=server.auth.password,
                timeout=SSH_CONNECT_TIMEOUT,
                banner_timeout=SSH_CONNECT_TIMEOUT,
                auth_timeout=SSH_CONNECT_TIMEOUT,
                look_for_keys=False,
                allow_agent=False,
            )
        else:
            pkey = load_private_key(server.auth.privateKey, server.auth.passphrase)
            client.connect(
                hostname=server.host,
                port=server.port,
                username=server.username,
                pkey=pkey,
                timeout=SSH_CONNECT_TIMEOUT,
                banner_timeout=SSH_CONNECT_TIMEOUT,
                auth_timeout=SSH_CONNECT_TIMEOUT,
                look_for_keys=False,
                allow_agent=False,
            )
        key = client.get_transport().get_remote_server_key()
        fingerprint = compute_fingerprint(key)
        if server.expectedFingerprint and server.expectedFingerprint != fingerprint:
            client.close()
            raise ValueError("Host fingerprint mismatch")
        return client
    except Exception:
        client.close()
        raise


def run_ssh_command(client: paramiko.SSHClient, command: str) -> Tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=SSH_COMMAND_TIMEOUT)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode()
    err = stderr.read().decode()
    return exit_code, out, err


def parse_output_json(output: str) -> Optional[Dict[str, Any]]:
    for line in output.splitlines():
        if line.startswith("OUTPUT_JSON:"):
            payload = line.split("OUTPUT_JSON:", 1)[1].strip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return None
    return None


def build_env(settings: Dict[str, Any], offline: OfflineConfig) -> Dict[str, str]:
    env = {}
    for key, value in settings.items():
        env_key = key.upper()
        env[env_key] = "" if value is None else str(value)
    if offline.enabled:
        env.update(
            {
                "OFFLINE": "1",
                "APT_PROXY": offline.aptProxy or "",
                "DOCKER_REGISTRY": offline.dockerRegistry or "",
                "ARTIFACT_MIRROR": offline.artifactMirror or "",
            }
        )
    return env


def env_export_string(env: Dict[str, str]) -> str:
    parts = [f"export {k}={shlex.quote(v)}" for k, v in env.items()]
    return "; ".join(parts)


def apply_defaults(manifest: TemplateManifest, settings: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(settings)
    for field_def in manifest.fields:
        key = field_def.get("key")
        if key is None:
            continue
        if key not in merged and "default" in field_def:
            merged[key] = field_def.get("default")
    return merged


async def execute_template(run: RunRecord, server: ServerModel, template: TemplateManifest, settings: Dict[str, Any], offline: OfflineConfig, step_index: int, total_steps: int) -> None:
    run.current_step = template.name
    step_progress = int((step_index / total_steps) * 100)
    run.progress = max(run.progress, step_progress)
    run.add_log("info", f"Starting {template.name}", progress=run.progress, step=template.name)

    secrets: List[str] = []
    if isinstance(server.auth, AuthPassword):
        secrets.append(server.auth.password)
    elif isinstance(server.auth, AuthKey):
        secrets.append(server.auth.privateKey)
        if server.auth.passphrase:
            secrets.append(server.auth.passphrase)

    try:
        client = ssh_connect(server)
    except Exception as e:
        run.add_log("error", mask_secret(f"SSH connect failed: {e}", secrets), step=template.name)
        raise

    try:
        env = build_env(settings, offline)
        env_exports = env_export_string(env)
        steps_script = template_loader.load_steps(template.id)
        command_lines = [
            "set -euo pipefail",
        ]
        if env_exports:
            command_lines.append(env_exports)
        command_lines.append("bash -s <<'EOF'")
        command_lines.append(steps_script)
        command_lines.append("EOF")
        command = "\n".join(command_lines)
        exit_code, out, err = run_ssh_command(client, command)
        if err.strip():
            masked_err = mask_secret(err, secrets)
            for line in masked_err.splitlines():
                run.add_log("info", line[:500], step=template.name)
        if out.strip():
            masked_out = mask_secret(out, secrets)
            for line in masked_out.splitlines():
                if line.startswith("OUTPUT_JSON:"):
                    continue
                run.add_log("info", line[:500], step=template.name)
        if exit_code != 0:
            raise RuntimeError(f"Template {template.id} failed with exit code {exit_code}")
        outputs = parse_output_json(out)
        if outputs:
            for key, val in outputs.items():
                run.outputs[key] = val
        run.progress = int(((step_index + 1) / total_steps) * 100)
        run.add_log("success", f"Finished {template.name}", progress=run.progress, step=template.name)
    finally:
        client.close()


async def resolve_dependencies(requested: List[TemplateSelection]) -> List[Tuple[TemplateManifest, Dict[str, Any]]]:
    manifests: Dict[str, TemplateManifest] = {}
    adjacency: Dict[str, Set[str]] = {}

    def add_template(template_id: str):
        if template_id in manifests:
            return
        manifest = template_loader.load(template_id)
        manifests[template_id] = manifest
        adjacency[template_id] = set(manifest.requires)
        for dep in manifest.requires:
            add_template(dep)

    for item in requested:
        add_template(item.id)

    in_degree: Dict[str, int] = {k: 0 for k in manifests}
    for template_id, deps in adjacency.items():
        for _ in deps:
            in_degree[template_id] = in_degree.get(template_id, 0) + 1
    queue: List[str] = [k for k, v in in_degree.items() if v == 0]
    ordered: List[str] = []
    while queue:
        node = queue.pop(0)
        ordered.append(node)
        for neighbor, deps in adjacency.items():
            if node in deps:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
    if len(ordered) != len(manifests):
        raise ValueError("Cyclic dependency in templates")

    selection_map = {item.id: item.settings for item in requested}
    result: List[Tuple[TemplateManifest, Dict[str, Any]]] = []
    for template_id in ordered:
        manifest = manifests[template_id]
        settings = apply_defaults(manifest, selection_map.get(template_id, {}))
        result.append((manifest, settings))
    return result


async def run_worker(run_id: str, request: RunRequest):
    run = await run_store.get(run_id)
    await run_store.acquire_slot()
    try:
        run.started_at = datetime.utcnow()
        run.status = "running"
        resolved = await resolve_dependencies(request.templates)
        total_steps = len(resolved)
        step_index = 0
        for manifest, settings in resolved:
            if run.status == "canceled":
                run.add_log("error", "Run canceled")
                break
            try:
                await execute_template(run, request.server, manifest, settings, request.offline, step_index, total_steps)
            except Exception as e:
                run.status = "failed"
                run.error = str(e)
                run.add_log("error", str(e), step=manifest.name)
                break
            step_index += 1
        else:
            run.status = "success"
            run.progress = 100
        run.finished_at = datetime.utcnow()
        run.add_log("info", "Run finished", progress=run.progress)
        for listener in list(run.listeners):
            try:
                listener.put_nowait(LogEntry(ts=datetime.utcnow(), level="done", message=run.status))
            except asyncio.QueueFull:
                continue
        # Wipe sensitive auth material
        try:
            request.server.auth = None  # type: ignore[assignment]
        except Exception:
            request.server.auth = None  # best-effort
    finally:
        run_store.release_slot()


async def cleanup_task():
    while True:
        await asyncio.sleep(60)
        await run_store.cleanup()


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(cleanup_task())


@app.post("/api/test-ssh")
async def test_ssh(payload: SSHTestRequest):
    server = payload
    try:
        client = ssh_connect(server)
    except ValueError as ve:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(ve)})
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

    try:
        key = client.get_transport().get_remote_server_key()
        fingerprint = compute_fingerprint(key)
        preflight = {}
        cmds = {
            "os": "lsb_release -ds || cat /etc/os-release",
            "sudo": "sudo -n true >/dev/null 2>&1 && echo yes || echo no",
            "cpu": "nproc",
            "ram": "awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo",
            "disk": "df -BG --output=avail / | tail -1 | tr -dc '0-9'",
        }
        _, os_out, _ = run_ssh_command(client, cmds["os"])
        preflight["os"] = os_out.strip().splitlines()[0] if os_out.strip() else "Unknown"
        _, sudo_out, _ = run_ssh_command(client, cmds["sudo"])
        preflight["sudo"] = sudo_out.strip().lower().startswith("yes")
        _, cpu_out, _ = run_ssh_command(client, cmds["cpu"])
        preflight["cpuCores"] = int(cpu_out.strip() or 0)
        _, ram_out, _ = run_ssh_command(client, cmds["ram"])
        preflight["ramMb"] = int(ram_out.strip() or 0)
        _, disk_out, _ = run_ssh_command(client, cmds["disk"])
        preflight["diskFreeGb"] = int(disk_out.strip() or 0)
        if server.expectedFingerprint and fingerprint != server.expectedFingerprint:
            return JSONResponse(status_code=400, content={"ok": False, "fingerprint": fingerprint, "error": "Fingerprint mismatch"})
        return {"ok": True, "fingerprint": fingerprint, "preflight": preflight}
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
    finally:
        client.close()


@app.get("/api/templates")
async def get_templates():
    templates = template_loader.list_templates()
    return [t.dict() for t in templates]


@app.post("/api/run")
async def start_run(request: RunRequest, background_tasks: BackgroundTasks):
    run_id = uuid.uuid4().hex
    run = RunRecord(id=run_id)
    await run_store.create_run(run)
    background_tasks.add_task(run_worker, run_id, request)
    return {"runId": run_id}


@app.get("/api/run/{run_id}")
async def get_run(run_id: str):
    try:
        run = await run_store.get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "id": run.id,
        "status": run.status,
        "progress": run.progress,
        "currentStep": run.current_step,
        "startedAt": run.started_at.isoformat() if run.started_at else None,
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
        "outputs": run.outputs,
        "error": run.error,
    }


@app.get("/api/run/{run_id}/stream")
async def stream_run(run_id: str):
    try:
        run = await run_store.get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    queue = run.subscribe()

    async def event_gen() -> AsyncGenerator[bytes, None]:
        try:
            for entry in list(run.logs):
                yield f"event: log\ndata: {json.dumps(entry.to_dict())}\n\n".encode()
            while True:
                entry = await queue.get()
                if isinstance(entry, LogEntry) and entry.level == "done":
                    yield f"event: done\ndata: {{\"status\": \"{entry.message}\"}}\n\n".encode()
                    break
                payload = entry.to_dict() if isinstance(entry, LogEntry) else {}
                yield f"event: log\ndata: {json.dumps(payload)}\n\n".encode()
        finally:
            run.unsubscribe(queue)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/api/run/{run_id}/cancel")
async def cancel_run(run_id: str):
    try:
        run = await run_store.get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status in {"success", "failed", "canceled"}:
        return {"status": run.status}
    run.status = "canceled"
    run.finished_at = datetime.utcnow()
    run.add_log("error", "Run cancellation requested")
    return {"status": "canceled"}


@app.get("/")
async def root():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8088, reload=False)
