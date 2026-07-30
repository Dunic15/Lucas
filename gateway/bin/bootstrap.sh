#!/usr/bin/env bash
# Provision the OpenClaw gateway host — DEVELOPMENT ONLY.
#
# Runs ON THE EC2 INSTANCE (as user-data or by hand). Nothing here depends on
# a laptop staying online: once this completes, the gateway survives reboots
# via `restart: unless-stopped` plus the systemd unit installed below.
#
# Secrets are pulled from SSM Parameter Store at boot into a root-owned 0600
# env file. They are never baked into an image, never in git, and never
# printed — `set -x` is deliberately NOT used.
set -euo pipefail

REGION="${AWS_REGION:-eu-central-1}"
SSM_PREFIX="${SSM_PREFIX:-/laura/next/openclaw}"
APP_DIR="/opt/openclaw"
SECRETS="${APP_DIR}/secrets.env"

log() { printf '[bootstrap] %s\n' "$*"; }

# ── development-only guard ────────────────────────────────────────────────
# Refuse to bootstrap if anything in the environment points at customer
# infrastructure. This is the same guard the deploy/teardown scripts use.
if command -v python3 >/dev/null 2>&1 && [ -f "${APP_DIR}/guards.py" ]; then
  python3 "${APP_DIR}/guards.py" \
    "${SSM_PREFIX}" "${GATEWAY_DOMAIN:-}" "${TARGET_SERVICE:-laura-backend-next}" \
    || { log "REFUSED by development-only guard"; exit 2; }
fi

log "installing docker + compose"
if command -v dnf >/dev/null 2>&1; then
  dnf -y install docker awscli-2
else
  apt-get update && apt-get -y install docker.io awscli
fi
systemctl enable --now docker

DOCKER_CLI_PLUGINS=/usr/local/lib/docker/cli-plugins
install -d "$DOCKER_CLI_PLUGINS"
if [ ! -x "$DOCKER_CLI_PLUGINS/docker-compose" ]; then
  # Pinned: a floating compose release can change file-format behaviour.
  curl -fsSL -o "$DOCKER_CLI_PLUGINS/docker-compose" \
    "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-$(uname -m)"
  chmod +x "$DOCKER_CLI_PLUGINS/docker-compose"
fi

# ── persistent state on the EBS data volume ───────────────────────────────
# Docker's data-root moves onto the data disk so named volumes (OpenClaw
# state, Caddy certs) live on EBS and are captured by snapshots.
DATA_DEV="${DATA_DEV:-/dev/nvme1n1}"
if [ -b "$DATA_DEV" ]; then
  blkid "$DATA_DEV" >/dev/null 2>&1 || mkfs.ext4 -F "$DATA_DEV"
  install -d /var/lib/docker
  grep -q "$DATA_DEV" /etc/fstab || echo "$DATA_DEV /var/lib/docker ext4 defaults,nofail 0 2" >> /etc/fstab
  mountpoint -q /var/lib/docker || { systemctl stop docker; mount -a; systemctl start docker; }
  log "state volume mounted at /var/lib/docker"
else
  log "WARNING: ${DATA_DEV} absent — state will live on the root volume"
fi

# ── secrets from SSM (never from git, never from the image) ───────────────
log "loading secrets from SSM ${SSM_PREFIX}"
install -d -m 0700 "$APP_DIR"
umask 077
: > "$SECRETS"
for name in ANTHROPIC_API_KEY OPENCLAW_GATEWAY_TOKEN; do
  value="$(aws ssm get-parameter --with-decryption --region "$REGION" \
            --name "${SSM_PREFIX}/${name}" \
            --query 'Parameter.Value' --output text 2>/dev/null || true)"
  if [ -z "$value" ] || [ "$value" = "None" ]; then
    log "FATAL: ${SSM_PREFIX}/${name} is missing in SSM"
    exit 3
  fi
  printf '%s=%s\n' "$name" "$value" >> "$SECRETS"
done
chmod 0600 "$SECRETS"
chown root:root "$SECRETS"
log "secrets written to ${SECRETS} (0600, root)"

# ── start, and keep it started across reboots ─────────────────────────────
cat > /etc/systemd/system/openclaw-gateway.service <<'UNIT'
[Unit]
Description=OpenClaw gateway (development only)
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/openclaw
EnvironmentFile=/opt/openclaw/gateway.env
ExecStart=/usr/bin/docker compose up -d --wait
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now openclaw-gateway.service
log "gateway up; verify with bin/health.sh"
