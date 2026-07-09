#!/usr/bin/env bash
# Boot wrapper for the Laura backend: restore the org-memory SQLite DB from S3,
# then run uvicorn UNDER Litestream so every write is continuously replicated.
#
#   Ephemeral App Runner disk + this wrapper = org memory survives deploys.
#
# This is variant (b) from docs/infra/STORAGE-DURABILITY.md §2a: keep the
# existing SOURCE-based App Runner deploy and fetch the Litestream binary at
# boot, instead of switching prod to an image/ECR deploy. The only prod change
# is pointing App Runner's StartCommand at this script + setting a few env vars
# (see docs/infra/STORAGE-ROLLOUT.md). Zero CI / Dockerfile changes.
#
# FAIL-OPEN — the live-meeting path must NEVER depend on S3 or a GitHub fetch.
# If replication is not configured (no LITESTREAM_REPLICA_URL) OR the binary
# can't be obtained OR restore fails, we log a warning and run uvicorn directly.
# The server ALWAYS boots. Worst case = this boot has no replication (exactly
# today's behaviour), never a boot that hangs or crashes on infra.
#
# Locally there is no LITESTREAM_REPLICA_URL, so this script is a transparent
# pass-through to `uvicorn` — the key-free demo is unchanged.

set -uo pipefail

# ── paths / config (env with safe defaults) ──────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PORT="${PORT:-8000}"
# Interpreter is overridable so a local venv can drive this script unchanged;
# App Runner's managed PYTHON_311 runtime provides `python3` on PATH.
PYTHON="${PYTHON:-python3}"
UVICORN_CMD="${PYTHON} -m uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT}"

# The app (backend/app/store.py) reads LAURA_STORE_PATH; Litestream must target
# the SAME file. Export it so the uvicorn child and Litestream agree.
: "${LAURA_STORE_PATH:=${REPO_ROOT}/backend/data/store.sqlite3}"
export LAURA_STORE_PATH

log() { echo "[start-with-litestream] $*" >&2; }

run_direct() {
  # Replication off (or unavailable): boot the server exactly as today.
  log "launching uvicorn directly — durability OFF"
  exec ${UVICORN_CMD}
}

# ── on/off switch: only engage Litestream when a replica URL is configured ───
if [ -z "${LITESTREAM_REPLICA_URL:-}" ]; then
  log "LITESTREAM_REPLICA_URL unset → org-memory durability disabled"
  run_direct
fi

# Defaults consumed (env-expanded) by etc/litestream.yml.
export LITESTREAM_REGION="${LITESTREAM_REGION:-eu-central-1}"
export LITESTREAM_SYNC_INTERVAL="${LITESTREAM_SYNC_INTERVAL:-10s}"
export LITESTREAM_RETENTION="${LITESTREAM_RETENTION:-168h}"
LITESTREAM_CONFIG="${LITESTREAM_CONFIG:-${REPO_ROOT}/etc/litestream.yml}"

# Pin a stable 0.5.x (NOT 0.5.0 — it had early-adopter bugs; see decision doc).
LITESTREAM_VERSION="${LITESTREAM_VERSION:-v0.5.14}"
LITESTREAM_BIN_DIR="${LITESTREAM_BIN_DIR:-/tmp/litestream-bin}"

# ── obtain the litestream binary (PATH → cache → pinned download) ─────────────
resolve_litestream() {
  # 1) already installed (e.g. baked into a future image)?
  if command -v litestream >/dev/null 2>&1; then
    LITESTREAM="$(command -v litestream)"; return 0
  fi
  # 2) cached from an earlier boot of this instance?
  if [ -x "${LITESTREAM_BIN_DIR}/litestream" ]; then
    LITESTREAM="${LITESTREAM_BIN_DIR}/litestream"; return 0
  fi
  # 3) download the pinned release.
  local os arch ver url
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"          # linux (App Runner)
  # 0.5.x asset naming: litestream-<version-no-v>-<os>-<x86_64|arm64>.tar.gz
  case "$(uname -m)" in
    x86_64|amd64)  arch="x86_64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) log "unsupported arch $(uname -m)"; return 1 ;;
  esac
  ver="${LITESTREAM_VERSION#v}"                            # tag v0.5.14 → file 0.5.14
  url="${LITESTREAM_DOWNLOAD_URL:-https://github.com/benbjohnson/litestream/releases/download/${LITESTREAM_VERSION}/litestream-${ver}-${os}-${arch}.tar.gz}"

  mkdir -p "${LITESTREAM_BIN_DIR}" || { log "cannot create ${LITESTREAM_BIN_DIR}"; return 1; }
  local tgz="${LITESTREAM_BIN_DIR}/litestream.tar.gz"
  log "downloading litestream ${LITESTREAM_VERSION} (${os}-${arch}) …"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --retry-delay 2 -o "$tgz" "$url" || { log "curl download failed"; return 1; }
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$tgz" "$url" || { log "wget download failed"; return 1; }
  else
    log "neither curl nor wget available"; return 1
  fi
  # Optional integrity check when the owner pins a checksum.
  if [ -n "${LITESTREAM_SHA256:-}" ] && command -v sha256sum >/dev/null 2>&1; then
    echo "${LITESTREAM_SHA256}  ${tgz}" | sha256sum -c - || { log "checksum MISMATCH"; return 1; }
  fi
  tar -xzf "$tgz" -C "${LITESTREAM_BIN_DIR}" litestream 2>/dev/null \
    || tar -xzf "$tgz" -C "${LITESTREAM_BIN_DIR}" || { log "extract failed"; return 1; }
  [ -x "${LITESTREAM_BIN_DIR}/litestream" ] || { log "binary missing after extract"; return 1; }
  LITESTREAM="${LITESTREAM_BIN_DIR}/litestream"; return 0
}

if ! resolve_litestream; then
  log "could not obtain litestream — falling back to direct boot (no replication)"
  run_direct
fi
log "using litestream at ${LITESTREAM} (config ${LITESTREAM_CONFIG})"

# ── restore-on-boot, then replicate-while-serving ────────────────────────────
# Fresh App Runner disk → the DB never exists → restore pulls the latest S3
# copy. Empty bucket on first ever boot → -if-replica-exists starts clean and
# the app creates the schema. Restore of a few-MB DB adds well under a second.
mkdir -p "$(dirname "${LAURA_STORE_PATH}")" 2>/dev/null || true
if ! "${LITESTREAM}" restore -config "${LITESTREAM_CONFIG}" \
      -if-replica-exists -if-db-not-exists "${LAURA_STORE_PATH}"; then
  log "restore errored — booting anyway (app will start on a fresh DB)"
fi

# Hand off to Litestream, which supervises uvicorn and ships WAL changes to S3
# for as long as the server runs. On uvicorn exit it does a final sync.
log "starting: litestream replicate -exec \"${UVICORN_CMD}\""
exec "${LITESTREAM}" replicate -config "${LITESTREAM_CONFIG}" -exec "${UVICORN_CMD}"
