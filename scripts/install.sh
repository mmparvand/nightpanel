#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/warops-backend
SERVICE_NAME=warops-backend
PORT=8088

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git rsync

sudo rm -rf "$APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo chown "$USER" "$APP_DIR"

rsync -a --exclude ".git" ./ "$APP_DIR"/
cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cat <<SERVICE | sudo tee /etc/systemd/system/${SERVICE_NAME}.service >/dev/null
[Unit]
Description=WarOps Backend
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
Environment=PATH=$APP_DIR/.venv/bin
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
Restart=on-failure

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl restart ${SERVICE_NAME}

IP=$(hostname -I | awk '{print $1}')
echo "Backend running on http://${IP}:${PORT}"
