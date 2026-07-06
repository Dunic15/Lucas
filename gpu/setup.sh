#!/bin/bash
# Laura photoreal GPU box — one-time setup (run on the EC2 instance).
#
# The box is launched from launch template `laura-gpu` (DLAMI Ubuntu 22.04,
# NVIDIA driver preinstalled, SSM-managed — no SSH keys). Code is pushed via
# SSM (repo is private), then this script installs everything and starts the
# server as a systemd service that survives reboots.
#
#   MODE=stub      -> transport-only server (works instantly, no downloads)
#   MODE=musetalk  -> full photoreal engine (downloads ~15GB of weights)
set -euo pipefail
MODE="${MODE:-stub}"
APP_DIR=/opt/laura-gpu
cd "$APP_DIR"

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install fastapi "uvicorn[standard]" pillow websockets

if [ "$MODE" = "musetalk" ]; then
  # MuseTalk (https://github.com/TMElyralab/MuseTalk) + weights from HF.
  # DLAMI ships CUDA; torch with cu121 matches the driver on this AMI.
  git clone https://github.com/TMElyralab/MuseTalk "$APP_DIR/MuseTalk"
  ./venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
  ./venv/bin/pip install -r "$APP_DIR/MuseTalk/requirements.txt"
  ./venv/bin/pip install "huggingface_hub[cli]"
  ./venv/bin/huggingface-cli download TMElyralab/MuseTalk --local-dir "$APP_DIR/MuseTalk/models"
  # NOTE (launch day): MuseTalk also needs whisper-tiny + sd-vae weights — its
  # download_weights.sh fetches all of them; run it if the HF pull above is
  # missing pieces, then wire musetalk_adapter.py (see README).
fi

# systemd service — GPU_IDLE_SHUTDOWN_MINUTES: the server stops the box itself
# when no page has been connected for 10 min (cost control, issue #3).
sudo tee /etc/systemd/system/laura-gpu.service > /dev/null <<EOF
[Unit]
Description=Laura photoreal avatar streaming server
After=network.target

[Service]
WorkingDirectory=$APP_DIR
Environment=AVATAR_ENGINE=$MODE
Environment=REFERENCE_IMAGE=$APP_DIR/assets/reference.jpg
Environment=GPU_IDLE_SHUTDOWN_MINUTES=10
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/server.py
Restart=always
User=ubuntu

[Install]
WantedBy=multi-user.target
EOF

# TTL dead-man switch: EVERY boot arms a hard 90-minute auto-stop, so the box
# can never be silently left running — no matter who started it (launch.sh,
# the backend's meeting autostart, or the console). launch.sh re-arms this
# with GPU_TTL_MINUTES for longer/shorter windows (shutdown -c + shutdown -h).
sudo tee /etc/systemd/system/laura-gpu-ttl.service > /dev/null <<EOF
[Unit]
Description=Hard TTL: auto-stop this GPU box 90 minutes after boot
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/shutdown -h +90

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now laura-gpu laura-gpu-ttl
echo "=== laura-gpu service running (mode=$MODE, idle-stop 10min, boot TTL 90min) ==="
curl -s localhost:8080/health
