#!/usr/bin/env bash
# Write the gateway secrets + non-secret config to SSM under /laura/dev/openclaw/*.
#
# Values are read from ENVIRONMENT VARIABLES (never command-line args) so they
# do not land in shell history. Nothing is echoed. Secrets are SecureString.
#
#   OPENCLAW_GATEWAY_TOKEN=...  ANTHROPIC_API_KEY=...  \
#   OPENCLAW_PUBLIC_HOSTNAME=openclaw-gw.dev.example.net  OPENCLAW_TLS_EMAIL=ops@example.net \
#   ./put-secrets.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${SECRETS_PATH_PREFIX:-/laura/dev/openclaw}"

case "${PREFIX}" in
  /laura/dev/*) : ;;
  *) echo "REFUSED: SECRETS_PATH_PREFIX must be under /laura/dev/* (got ${PREFIX})" >&2
     exit 2 ;;
esac

put_secure() {  # name value
  [ -n "${2:-}" ] || { echo "skip ${1} (unset)"; return 0; }
  aws ssm put-parameter --name "${PREFIX}/$1" --type SecureString \
    --value "$2" --overwrite ${AWS_REGION:+--region "${AWS_REGION}"} >/dev/null
  echo "put SecureString ${PREFIX}/$1"
}
put_string() {  # name value
  [ -n "${2:-}" ] || { echo "skip ${1} (unset)"; return 0; }
  aws ssm put-parameter --name "${PREFIX}/$1" --type String \
    --value "$2" --overwrite ${AWS_REGION:+--region "${AWS_REGION}"} >/dev/null
  echo "put String ${PREFIX}/$1"
}

put_secure token                "${OPENCLAW_GATEWAY_TOKEN:-}"
put_secure anthropic_api_key    "${ANTHROPIC_API_KEY:-}"
put_secure cloudflared_token    "${CLOUDFLARED_TOKEN:-}"
put_string public_hostname      "${OPENCLAW_PUBLIC_HOSTNAME:-}"

echo "Done. The instance reads these at boot with least-privilege ssm:GetParameter."
