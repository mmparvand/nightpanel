#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "$1"
}

if [[ "${OFFLINE:-0}" == "1" && -n "${APT_PROXY:-}" ]]; then
  echo "Acquire::http::Proxy \"${APT_PROXY}\";" | sudo tee /etc/apt/apt.conf.d/01proxy >/dev/null
  echo "Using APT proxy ${APT_PROXY}"
fi

log "Updating apt lists"
sudo apt-get update -y
log "Upgrading packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

log "System update complete"
