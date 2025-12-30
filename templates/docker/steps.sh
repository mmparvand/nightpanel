#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "$1"
}

if [[ "${OFFLINE:-0}" == "1" && -n "${APT_PROXY:-}" ]]; then
  echo "Acquire::http::Proxy \"${APT_PROXY}\";" | sudo tee /etc/apt/apt.conf.d/01proxy >/dev/null
  echo "Using APT proxy ${APT_PROXY}"
fi

log "Installing docker.io"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
sudo systemctl enable docker
sudo systemctl start docker

log "Docker installation complete"
