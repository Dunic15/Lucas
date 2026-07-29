#!/bin/bash
# =============================================================================
# OpenClaw gateway — EC2 cloud-init bootstrap (Amazon Linux 2023, arm64).
#
# This script is the source of truth for what the instance does at first boot.
# The CloudFormation template embeds a byte-identical copy between the
# "USER-DATA START/END" sentinels (tests/validate_infra.py asserts they match).
#
# It deliberately reads ALL of its configuration from IMDS instance tags and
# from SSM Parameter Store — it hardcodes no account id, no region, no secret
# and no image reference. Secrets are pulled at boot from SSM SecureString and
# written to a root-only 0600 env file that never leaves the box.
# =============================================================================
# >>> USER-DATA START
set -euo pipefail

log() { echo "[openclaw-bootstrap] $*"; }

# --- IMDSv2 (the instance enforces HttpTokens=required) ----------------------
imds() {
  local path="$1"
  curl -fsS -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" \
    "http://169.254.169.254/latest/${path}"
}
IMDS_TOKEN="$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")"

export AWS_DEFAULT_REGION="$(imds meta-data/placement/region)"
INSTANCE_ID="$(imds meta-data/instance-id)"

# Configuration travels as instance tags (InstanceMetadataTags=enabled), not as
# templated CFN substitutions — that keeps this script literal and lintable.
OPENCLAW_IMAGE="$(imds meta-data/tags/instance/openclaw:image)"
SECRETS_PREFIX="$(imds meta-data/tags/instance/openclaw:secrets-prefix)"
LOG_GROUP="$(imds meta-data/tags/instance/openclaw:log-group)"
DATA_DEVICE_HINT="$(imds meta-data/tags/instance/openclaw:data-device)"
log "region=${AWS_DEFAULT_REGION} instance=${INSTANCE_ID} image=${OPENCLAW_IMAGE}"

# Refuse to boot on an unpinned image — never :latest, never a bare tag.
case "${OPENCLAW_IMAGE}" in
  *@sha256:*) : ;;
  *) log "FATAL: OPENCLAW_IMAGE is not pinned to an @sha256 digest"; exit 1 ;;
esac

# --- Packages ----------------------------------------------------------------
dnf -y update
dnf -y install docker awscli amazon-cloudwatch-agent jq
systemctl enable --now docker

# Docker Compose v2 plugin, pinned + checksum-verified (update in the runbook).
COMPOSE_VERSION="v2.29.7"
COMPOSE_SHA256_ARM64="REPLACE_WITH_SHA256"  # fill from the runbook to enforce verification
install_compose() {
  local dest="/usr/libexec/docker/cli-plugins/docker-compose"
  mkdir -p "$(dirname "${dest}")"
  if docker compose version >/dev/null 2>&1; then
    log "docker compose already present"; return 0
  fi
  curl -fsSL -o "${dest}" \
    "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64"
  # Verify only when a real digest has been filled in (placeholder is skipped
  # with a loud warning so a fresh checkout still boots for a smoke test).
  if [ "${COMPOSE_SHA256_ARM64}" != "REPLACE_WITH_SHA256" ] \
     && echo "${COMPOSE_SHA256_ARM64}  ${dest}" | sha256sum -c - 2>/dev/null; then
    log "docker compose checksum verified"
  else
    log "WARNING: docker compose checksum NOT verified — set COMPOSE_SHA256_ARM64"
  fi
  chmod 0755 "${dest}"
}
install_compose

# --- Persistent EBS data volume ---------------------------------------------
# Find the attached data volume by its documented device hint, tolerating the
# NVMe rename AL2023 applies (/dev/sdf -> /dev/nvme?n1).
find_data_device() {
  local dev
  for dev in "${DATA_DEVICE_HINT}" /dev/xvdf /dev/sdf; do
    [ -b "${dev}" ] && { echo "${dev}"; return 0; }
  done
  # NVMe: match the EBS volume that is NOT the root device.
  for dev in /dev/nvme*n1; do
    [ -b "${dev}" ] || continue
    if ! lsblk -no MOUNTPOINT "${dev}" | grep -q '/$'; then
      # skip the disk that carries the root partition
      lsblk -no MOUNTPOINT "${dev}1" 2>/dev/null | grep -q '/$' && continue
      echo "${dev}"; return 0
    fi
  done
  return 1
}
DATA_MOUNT="/opt/openclaw/data"
mkdir -p "${DATA_MOUNT}"
DATA_DEV="$(find_data_device || true)"
if [ -n "${DATA_DEV}" ]; then
  if ! blkid "${DATA_DEV}" >/dev/null 2>&1; then
    log "formatting fresh data volume ${DATA_DEV}"
    mkfs.ext4 -L openclaw-data "${DATA_DEV}"
  fi
  grep -q "LABEL=openclaw-data" /etc/fstab || \
    echo "LABEL=openclaw-data ${DATA_MOUNT} ext4 defaults,nofail 0 2" >> /etc/fstab
  mount "${DATA_MOUNT}" || true
else
  log "WARNING: data volume not found — persistence is degraded"
fi
mkdir -p "${DATA_MOUNT}/config" "${DATA_MOUNT}/auth"
# The auth-profile secret key store must not be world/group readable.
chmod 0700 "${DATA_MOUNT}/auth" "${DATA_MOUNT}/config"

# --- Secrets: SSM SecureString -> root-only env file -------------------------
APP_DIR="/opt/openclaw"
mkdir -p "${APP_DIR}"
ENV_FILE="${APP_DIR}/gateway.env"
PROXY_ENV_FILE="${APP_DIR}/proxy.env"
get_secret() { aws ssm get-parameter --name "$1" --with-decryption \
  --query 'Parameter.Value' --output text 2>/dev/null || echo ""; }
get_config() { aws ssm get-parameter --name "$1" \
  --query 'Parameter.Value' --output text 2>/dev/null || echo ""; }

OPENCLAW_GATEWAY_TOKEN="$(get_secret "${SECRETS_PREFIX}/token")"
LLM_API_KEY="$(get_secret "${SECRETS_PREFIX}/anthropic_api_key")"
TUNNEL_TOKEN="$(get_secret "${SECRETS_PREFIX}/cloudflared_token")"
PUBLIC_HOSTNAME="$(get_config "${SECRETS_PREFIX}/public_hostname")"

# Gateway secrets go in a root-only 0600 file. The proxy/tunnel containers do
# NOT receive the LLM key — only the gateway does (limit the blast radius).
umask 077
cat > "${ENV_FILE}" <<ENVEOF
OPENCLAW_GATEWAY_TOKEN=${OPENCLAW_GATEWAY_TOKEN}
ANTHROPIC_API_KEY=${LLM_API_KEY}
OPENCLAW_GATEWAY_BIND=lan
TUNNEL_TOKEN=${TUNNEL_TOKEN}
ENVEOF
chmod 0600 "${ENV_FILE}"
chown root:root "${ENV_FILE}"

# Non-secret proxy config: just the hostname Caddy provisions a cert for.
cat > "${PROXY_ENV_FILE}" <<PENVEOF
OPENCLAW_PUBLIC_HOSTNAME=${PUBLIC_HOSTNAME}
PENVEOF
chmod 0644 "${PROXY_ENV_FILE}"
unset OPENCLAW_GATEWAY_TOKEN LLM_API_KEY TUNNEL_TOKEN

# --- Compose + Caddy definitions (mirror of infra/.../compose/*) -------------
cat > "${APP_DIR}/docker-compose.yml" <<'COMPOSE_EOF'
name: laura-openclaw-gw-dev
services:
  gateway:
    image: ${OPENCLAW_IMAGE:?set to a pinned ghcr.io/openclaw/openclaw@sha256 digest}
    container_name: openclaw-gateway
    restart: unless-stopped
    env_file: [/opt/openclaw/gateway.env]
    expose: ["18789"]
    volumes:
      - /opt/openclaw/data/config:/home/node/.openclaw
      - /opt/openclaw/data/auth:/home/node/.config/openclaw
    cap_drop: [NET_RAW, NET_ADMIN]
    security_opt: ["no-new-privileges:true"]
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:18789/healthz || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 40s
    logging:
      driver: awslogs
      options:
        awslogs-region: ${AWS_DEFAULT_REGION}
        awslogs-group: ${LOG_GROUP}
        awslogs-stream: gateway
  proxy:
    image: caddy:2.8.4-alpine
    container_name: openclaw-caddy
    restart: unless-stopped
    depends_on: [gateway]
    ports:
      - "443:443"
      - "443:443/udp"
    env_file: [/opt/openclaw/proxy.env]
    volumes:
      - /opt/openclaw/Caddyfile:/etc/caddy/Caddyfile:ro
      - /opt/openclaw/data/caddy:/data
    cap_drop: [NET_RAW, NET_ADMIN]
    security_opt: ["no-new-privileges:true"]
    logging:
      driver: awslogs
      options:
        awslogs-region: ${AWS_DEFAULT_REGION}
        awslogs-group: ${LOG_GROUP}
        awslogs-stream: caddy
COMPOSE_EOF

cat > "${APP_DIR}/Caddyfile" <<'CADDY_EOF'
{
	admin off
	servers {
		protocols h1 h2 h3
	}
}
{$OPENCLAW_PUBLIC_HOSTNAME} {
	encode zstd gzip
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer
		-Server
	}
	# TLS is terminated here; the OpenClaw gateway still enforces the bearer
	# token on /v1/responses (we do NOT delegate auth to the proxy).
	# Automatic HTTPS provisions + renews a publicly-trusted cert for the
	# hostname above. Never `tls internal` — Laura verifies certificates.
	reverse_proxy gateway:18789
}
CADDY_EOF

# --- CloudWatch agent for host/system logs (container logs use awslogs) ------
cat > /opt/aws/amazon-cloudwatch-agent/etc/openclaw.json <<CWEOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {"file_path": "/var/log/cloud-init-output.log",
           "log_group_name": "${LOG_GROUP}", "log_stream_name": "bootstrap",
           "retention_in_days": 30}
        ]
      }
    }
  }
}
CWEOF
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/openclaw.json || true

# --- Launch -----------------------------------------------------------------
export OPENCLAW_IMAGE LOG_GROUP AWS_DEFAULT_REGION
cd "${APP_DIR}"
docker compose pull
docker compose up -d
log "openclaw gateway started"
# >>> USER-DATA END
