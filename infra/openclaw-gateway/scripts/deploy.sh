#!/usr/bin/env bash
# Deploy the OpenClaw DEV gateway CloudFormation stack.
#
# Guard-first: refuses any stack name that is not a recognised dev resource, and
# refuses an OpenClaw image that is not pinned to an @sha256 digest. Renders the
# template by injecting scripts/user-data.sh (single source of truth) into the
# UserData placeholder, then runs `aws cloudformation deploy`.
#
# This script is authored for a human operator to run against a DEV account.
# It performs an AWS change — review the plan before running.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE="${INFRA_DIR}/cloudformation/openclaw-gateway.yaml"
USER_DATA="${SCRIPT_DIR}/user-data.sh"

STACK_NAME="${1:-}"
PARAMS_FILE="${2:-${INFRA_DIR}/config/params.env}"

if [ -z "${STACK_NAME}" ]; then
  echo "usage: deploy.sh <stack-name> [params-file]" >&2
  echo "       (stack-name must be a dev resource, e.g. laura-openclaw-gw-dev)" >&2
  exit 2
fi

# 1) Dev-only guard — fails closed on customer/unknown names.
python3 "${SCRIPT_DIR}/dev_guard.py" "${STACK_NAME}"

# 2) Load non-secret parameters.
if [ ! -f "${PARAMS_FILE}" ]; then
  echo "missing params file: ${PARAMS_FILE} (copy config/params.example.env)" >&2
  exit 2
fi
# shellcheck disable=SC1090
set -a; . "${PARAMS_FILE}"; set +a

# 3) Enforce a pinned image before we ever call AWS.
case "${OpenClawImage:-}" in
  ghcr.io/openclaw/openclaw*@sha256:*) : ;;
  *) echo "REFUSED: OpenClawImage must be ghcr.io/openclaw/openclaw...@sha256:<digest>" >&2
     echo "         got: '${OpenClawImage:-<unset>}' (never :latest / :main)" >&2
     exit 2 ;;
esac
python3 "${SCRIPT_DIR}/dev_guard.py" "${SecretsPathPrefix:-/laura/dev/openclaw}" >/dev/null 2>&1 || {
  case "${SecretsPathPrefix:-/laura/dev/openclaw}" in
    /laura/dev/*) : ;;
    *) echo "REFUSED: SecretsPathPrefix must live under /laura/dev/*" >&2; exit 2 ;;
  esac
}

# 4) Render: base64 the user-data (portable across macOS/Linux) and inject it.
B64="$(base64 < "${USER_DATA}" | tr -d '\n')"
RENDERED="$(mktemp -t openclaw-gw.XXXXXX.yaml)"
trap 'rm -f "${RENDERED}"' EXIT
# Use python for a literal, escaping-safe substitution of the placeholder.
USER_DATA_B64="${B64}" TEMPLATE_IN="${TEMPLATE}" python3 - "${RENDERED}" <<'PY'
import os, sys
out = sys.argv[1]
src = open(os.environ["TEMPLATE_IN"]).read()
src = src.replace("@@USER_DATA_B64@@", os.environ["USER_DATA_B64"])
open(out, "w").write(src)
PY

echo "Deploying stack '${STACK_NAME}' in ${AWS_REGION:-<default region>}..."
aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${RENDERED}" \
  --capabilities CAPABILITY_NAMED_IAM \
  ${AWS_REGION:+--region "${AWS_REGION}"} \
  --parameter-overrides \
    EnvironmentName="${EnvironmentName:-dev}" \
    NamePrefix="${NamePrefix:-laura-openclaw-gw-dev}" \
    VpcId="${VpcId}" \
    SubnetId="${SubnetId}" \
    AvailabilityZone="${AvailabilityZone}" \
    InstanceType="${InstanceType:-t4g.small}" \
    OpenClawImage="${OpenClawImage}" \
    DataVolumeSizeGb="${DataVolumeSizeGb:-20}" \
    DataVolumeSnapshotId="${DataVolumeSnapshotId:-}" \
    LogRetentionInDays="${LogRetentionInDays:-30}" \
    SecretsPathPrefix="${SecretsPathPrefix:-/laura/dev/openclaw}" \
    AllowedHttpsCidr="${AllowedHttpsCidr:-127.0.0.1/32}"

echo "Done. Next: point DNS at the Elastic IP (or start the tunnel), then run scripts/smoke.sh."
