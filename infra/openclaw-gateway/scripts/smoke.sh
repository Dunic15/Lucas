#!/usr/bin/env bash
# Smoke test the deployed gateway: health endpoints + /v1/responses auth.
#
#   OPENCLAW_GATEWAY_TOKEN=... ./smoke.sh https://openclaw-gw.dev.example.net
#
# The token is read from the environment and NEVER printed. Exit 0 = all pass.
set -euo pipefail

BASE="${1:-}"
if [ -z "${BASE}" ]; then
  echo "usage: OPENCLAW_GATEWAY_TOKEN=... smoke.sh <https-base-url>" >&2
  exit 2
fi
BASE="${BASE%/}"
TOKEN="${OPENCLAW_GATEWAY_TOKEN:-}"
fail=0

BODY='{"model":"openclaw","input":"smoke","max_output_tokens":1}'

get_code() {  # url
  curl -s -o /dev/null -w '%{http_code}' "$1"
}
post_code() {  # url [token]
  local url="$1" token="${2:-}"
  local args=(-s -o /dev/null -w '%{http_code}' -X POST
              -H 'Content-Type: application/json' --data "${BODY}")
  [ -n "${token}" ] && args+=(-H "Authorization: Bearer ${token}")
  curl "${args[@]}" "${url}"
}

check() {  # label expected-regex actual
  if [[ "$3" =~ $2 ]]; then echo "PASS  $1 -> $3"
  else echo "FAIL  $1 -> $3 (expected $2)"; fail=1; fi
}

echo "== liveness / readiness =="
check "GET /healthz" '^(200|204)$' "$(get_code "${BASE}/healthz")"
check "GET /readyz"  '^(200|204)$' "$(get_code "${BASE}/readyz")"

echo "== /v1/responses auth is mandatory =="
# No/invalid bearer MUST be rejected.
check "POST /v1/responses (no token)"  '^(401|403)$' "$(post_code "${BASE}/v1/responses")"

if [ -n "${TOKEN}" ]; then
  # A valid bearer must NOT be 401/403 (200, or a downstream 4xx/5xx, but authed).
  got="$(post_code "${BASE}/v1/responses" "${TOKEN}")"
  if [[ "${got}" =~ ^(401|403)$ ]]; then
    echo "FAIL  POST /v1/responses (valid token) -> ${got} (token rejected)"; fail=1
  else
    echo "PASS  POST /v1/responses (valid token) -> ${got} (authenticated)"
  fi
else
  echo "SKIP  valid-token check (OPENCLAW_GATEWAY_TOKEN not set)"
fi

[ "${fail}" -eq 0 ] && echo "ALL SMOKE CHECKS PASSED" || echo "SMOKE FAILURES ABOVE"
exit "${fail}"
