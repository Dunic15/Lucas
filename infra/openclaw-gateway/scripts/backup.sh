#!/usr/bin/env bash
# Create an on-demand EBS snapshot of the gateway's persistent data volume.
# Restore path: pass the printed snapshot id back as DataVolumeSnapshotId when
# deploying (see restore.sh). Guard-first on the stack name.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_NAME="${1:-laura-openclaw-gw-dev}"
python3 "${SCRIPT_DIR}/dev_guard.py" "${STACK_NAME}"

VOL_ID="$(aws cloudformation describe-stack-resources \
  --stack-name "${STACK_NAME}" \
  ${AWS_REGION:+--region "${AWS_REGION}"} \
  --query "StackResources[?LogicalResourceId=='DataVolume'].PhysicalResourceId" \
  --output text)"
[ -n "${VOL_ID}" ] || { echo "could not find DataVolume in ${STACK_NAME}" >&2; exit 2; }

SNAP_ID="$(aws ec2 create-snapshot \
  --volume-id "${VOL_ID}" \
  ${AWS_REGION:+--region "${AWS_REGION}"} \
  --description "openclaw-gw dev backup ${STACK_NAME}" \
  --tag-specifications 'ResourceType=snapshot,Tags=[{Key=laura:environment,Value=dev},{Key=Name,Value=openclaw-gw-dev-backup}]' \
  --query SnapshotId --output text)"
echo "Snapshot started: ${SNAP_ID} (from ${VOL_ID})"
echo "Restore with: DataVolumeSnapshotId=${SNAP_ID} in config/params.env, then scripts/deploy.sh"
