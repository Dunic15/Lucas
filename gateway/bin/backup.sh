#!/usr/bin/env bash
# Back up OpenClaw state + Caddy certs — DEVELOPMENT ONLY.
# Runs on the instance. Writes a timestamped tarball and optionally copies it
# to S3. Secrets are NOT included: they live in SSM and are re-fetched on
# restore, so a stolen backup cannot leak an API key.
set -euo pipefail
OUT_DIR="${BACKUP_DIR:-/opt/openclaw/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${OUT_DIR}/openclaw-state-${STAMP}.tar.gz"
install -d "$OUT_DIR"
echo "[backup] pausing writers for a consistent copy"
docker compose stop openclaw
trap 'docker compose start openclaw' EXIT
docker run --rm \
  -v openclaw_openclaw-state:/state:ro \
  -v openclaw_caddy-data:/caddy:ro \
  -v "${OUT_DIR}:/out" \
  alpine@sha256:2222222222222222222222222222222222222222222222222222222222222222 \
  tar czf "/out/$(basename "$ARCHIVE")" -C / state caddy
echo "[backup] wrote ${ARCHIVE}"
if [ -n "${BACKUP_S3_URI:-}" ]; then
  aws s3 cp "$ARCHIVE" "${BACKUP_S3_URI%/}/" --sse aws:kms
  echo "[backup] copied to ${BACKUP_S3_URI}"
fi
