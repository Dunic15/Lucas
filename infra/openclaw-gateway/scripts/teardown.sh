#!/usr/bin/env bash
# Tear down the OpenClaw DEV gateway stack. Guard-first.
#
# BEFORE running: flip Laura's experiment OFF for the dev org (see the runbook,
# "Rollback to disabled experiment") so nothing tries to reach a gateway that is
# about to disappear. The data volume's DeletionPolicy is Snapshot, so deleting
# the stack leaves a backup snapshot behind (delete it manually when sure).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_NAME="${1:-}"
if [ -z "${STACK_NAME}" ]; then
  echo "usage: teardown.sh <stack-name>" >&2
  exit 2
fi
python3 "${SCRIPT_DIR}/dev_guard.py" "${STACK_NAME}"

echo "This deletes stack '${STACK_NAME}'. The data volume will be SNAPSHOTTED first."
read -r -p "Type the stack name to confirm: " confirm
[ "${confirm}" = "${STACK_NAME}" ] || { echo "aborted"; exit 1; }

aws cloudformation delete-stack --stack-name "${STACK_NAME}" \
  ${AWS_REGION:+--region "${AWS_REGION}"}
echo "delete-stack requested. Watch: aws cloudformation describe-stacks --stack-name ${STACK_NAME}"
echo "Reminder: the DataVolume snapshot and the SSM /laura/dev/openclaw/* secrets remain — remove when done."
