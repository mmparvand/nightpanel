#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "$1"
}

PORT=${WEBPORT:-8443}
ADMIN_USER=${ADMINUSER:-admin}
ADMIN_PASS=${ADMINPASS:-AUTO}
DOMAIN=${DOMAIN:-}

if [[ "$ADMIN_PASS" == "AUTO" ]]; then
  ADMIN_PASS=$(openssl rand -base64 24)
fi

IMAGE="nginx:stable"
if [[ "${OFFLINE:-0}" == "1" && -n "${DOCKER_REGISTRY:-}" ]]; then
  IMAGE="${DOCKER_REGISTRY%/}/nginx:stable"
fi

log "Ensuring docker is running"
sudo systemctl start docker

log "Pulling image ${IMAGE}"
sudo docker pull "$IMAGE" >/dev/null 2>&1 || true

log "Starting container on port ${PORT}"
sudo docker rm -f example-panel >/dev/null 2>&1 || true
sudo docker run -d --name example-panel -p ${PORT}:80 "$IMAGE"

URL="http://$(hostname -I | awk '{print $1}'):${PORT}"
if [[ -n "$DOMAIN" ]]; then
  URL="http://${DOMAIN}:${PORT}"
fi

log "Container started"

OUTPUT_JSON: {"serviceUrl":"${URL}","ports":"${PORT}:80","adminUser":"${ADMIN_USER}","adminPass":"${ADMIN_PASS}"}
