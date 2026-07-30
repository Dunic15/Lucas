#!/usr/bin/env bash
# Restore OpenClaw state from a backup archive — DEVELOPMENT ONLY.
# Secrets are NOT restored from the archive; bootstrap.sh re-fetches them
# from SSM, so restoring cannot resurrect a rotated credential.
set -euo pipefail
ARCHIVE="${1:?usage: restore.sh <archive.tar.gz>}"
[ -f "$ARCHIVE" ] || { echo "no such archive: $ARCHIVE"; exit 1; }
echo "[restore] stopping gateway"
docker compose down
docker run --rm \
  -v openclaw_openclaw-state:/state \
  -v openclaw_caddy-data:/caddy \
  -v "$(cd "$(dirname "$ARCHIVE")" && pwd):/in:ro" \
  alpine@sha256:2222222222222222222222222222222222222222222222222222222222222222 \
  sh -c "rm -rf /state/* /caddy/* && tar xzf /in/$(basename "$ARCHIVE") -C /"
echo "[restore] restarting"
docker compose up -d --wait
echo "[restore] done; verify with bin/health.sh"
