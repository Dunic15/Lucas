#!/usr/bin/env bash
# Safely add the OpenClaw variable NAMES to the laura-backend-next App Runner
# service using the describe -> merge -> update pattern. An `update-service`
# REPLACES the whole env map, so we must read the current config, merge, and
# write the union — never send a partial map.
#
# Guard-first: refuses any service that is not laura-backend-next (the customer
# `laura-backend` / frozen line is rejected). This script PRINTS the merged
# config and the exact update-service command but does NOT run the update unless
# you pass --apply, so you can review the diff first.
#
# Values for the OpenClaw vars are NOT set here (no secrets in the tree). Set
# them as App Runner runtime env or, preferably, as RuntimeEnvironmentSecrets
# pointing at SSM. This script only guarantees the NAMES are present.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${1:-laura-backend-next}"
APPLY="${2:-}"
REGION="${AWS_REGION:-eu-central-1}"

# 1) Guard: only the dev backend service may be touched.
python3 "${SCRIPT_DIR}/dev_guard.py" "${SERVICE_NAME}"

# 2) Never deploy over a live meeting — the caller must confirm active_sessions==0.
echo "PRECHECK: confirm 'GET /health' on laura-backend-next shows active_sessions:0"
echo "PRECHECK: confirm no concurrent op: aws apprunner list-operations --service-arn <arn>"
echo

# 3) Resolve the service ARN by NAME.
SERVICE_ARN="$(aws apprunner list-services --region "${REGION}" \
  --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn | [0]" \
  --output text)"
[ -n "${SERVICE_ARN}" ] && [ "${SERVICE_ARN}" != "None" ] || {
  echo "service not found: ${SERVICE_NAME}" >&2; exit 2; }

# 4) Describe -> merge the OpenClaw NAMES into the existing env map.
DESCRIBE="$(aws apprunner describe-service --region "${REGION}" --service-arn "${SERVICE_ARN}")"
MERGED="$(SERVICE_JSON="${DESCRIBE}" python3 - <<'PY'
import json, os
svc = json.loads(os.environ["SERVICE_JSON"])["Service"]
cfg = svc["SourceConfiguration"]["ImageRepository"]["ImageConfiguration"]
env = dict(cfg.get("RuntimeEnvironmentVariables") or {})
# Only ADD names that are missing; never clobber an existing value.
defaults = {
    "OPENCLAW_EXPERIMENT_ENABLED": "false",   # stays OFF until you flip it
    "OPENCLAW_EXPERIMENT_ORGS": "",           # the DEV org id, or *
    "OPENCLAW_AUTO_RUN": "false",
    "OPENCLAW_GATEWAY_URL": "",               # https://<gateway-host>
    "OPENCLAW_GATEWAY_TOKEN": "",             # prefer a RuntimeEnvironmentSecret
    "OPENCLAW_AGENT_ID": "laura-executor-test",
    "OPENCLAW_BROWSER_ENABLED": "true",
}
added = [k for k in defaults if k not in env]
for k in added:
    env[k] = defaults[k]
cfg["RuntimeEnvironmentVariables"] = env
out = {"SourceConfiguration": svc["SourceConfiguration"]}
print(json.dumps({"added": added, "source": out}, indent=2))
PY
)"
echo "${MERGED}"

SRC_FILE="$(mktemp -t la-next-src.XXXXXX.json)"
echo "${MERGED}" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["source"]))' > "${SRC_FILE}"

CMD=(aws apprunner update-service --region "${REGION}"
     --service-arn "${SERVICE_ARN}"
     --source-configuration "file://${SRC_FILE}")
if [ "${APPLY}" = "--apply" ]; then
  echo "Applying update-service to ${SERVICE_NAME}..."
  "${CMD[@]}"
else
  echo
  echo "DRY RUN. Review the added names above, then run:"
  printf '  %q ' "${CMD[@]}"; echo
  echo "(or re-run this script with --apply)"
fi
