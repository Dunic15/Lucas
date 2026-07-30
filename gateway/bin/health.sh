#!/usr/bin/env bash
# Gateway health — DEVELOPMENT ONLY. Read-only; safe to run anywhere.
set -euo pipefail
DOMAIN="${GATEWAY_DOMAIN:?set GATEWAY_DOMAIN}"
TOKEN="${OPENCLAW_GATEWAY_TOKEN:-}"
echo "[health] TLS + liveness"
curl -fsS --max-time 10 "https://${DOMAIN}/health" && echo
echo "[health] auth is mandatory (expect 401 without a token)"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  -X POST "https://${DOMAIN}/v1/responses" -d '{}')"
[ "$code" = "401" ] || { echo "[health] FAIL: unauthenticated POST returned ${code}, expected 401"; exit 1; }
echo "[health] ok: unauthenticated request rejected"
if [ -n "$TOKEN" ]; then
  echo "[health] authenticated probe"
  curl -s -o /dev/null -w '  /v1/responses -> %{http_code}\n' --max-time 20 \
    -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
    -X POST "https://${DOMAIN}/v1/responses" -d '{"model":"anthropic/claude-opus-5","input":"ping"}'
fi
echo "[health] container state"; docker compose ps 2>/dev/null || true
