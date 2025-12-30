# WarOps Backend

FastAPI backend with in-memory run engine and pluggable templates.

## Quick start (dev)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8088
```

## Endpoints
- `POST /api/test-ssh`
- `GET /api/templates`
- `POST /api/run`
- `GET /api/run/{id}`
- `GET /api/run/{id}/stream`
- `POST /api/run/{id}/cancel`

## Example curl

Test SSH:
```bash
curl -X POST http://localhost:8088/api/test-ssh \
 -H 'Content-Type: application/json' \
 -d '{"host":"1.1.1.1","port":22,"username":"root","auth":{"type":"password","password":"secret"}}'
```

List templates:
```bash
curl http://localhost:8088/api/templates
```

Start run:
```bash
curl -X POST http://localhost:8088/api/run \
 -H 'Content-Type: application/json' \
 -d '{"server":{"host":"1.1.1.1","port":22,"username":"root","auth":{"type":"password","password":"secret"}},"templates":[{"id":"system-update","settings":{}},{"id":"docker","settings":{}},{"id":"example-panel","settings":{"webPort":8443}}],"offline":{"enabled":false}}'
```

Stream logs:
```bash
curl http://localhost:8088/api/run/<runId>/stream
```

## Templates
Add new templates under `templates/<id>` with `manifest.json` and `steps.sh`.

## Notes
- No persistence; run state kept in RAM with 30m TTL.
- Secrets are masked in logs and not stored after run completion.
