#!/usr/bin/env bash
# Restore the data volume from an EBS snapshot.
#
# Restoring REPLACES the data volume, so CloudFormation cannot swap it in place
# without recreating the instance. The safe, explicit flow:
#   1) scripts/teardown.sh <stack>        # snapshots the current volume, deletes the stack
#   2) set DataVolumeSnapshotId=<snap> in config/params.env
#   3) scripts/deploy.sh <stack>          # new instance boots from the snapshot
# This script just validates the inputs and prints those steps — it does not
# perform a destructive in-place swap.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_NAME="${1:-laura-openclaw-gw-dev}"
SNAP_ID="${2:-}"
python3 "${SCRIPT_DIR}/dev_guard.py" "${STACK_NAME}"

case "${SNAP_ID}" in
  snap-*) : ;;
  *) echo "usage: restore.sh <stack-name> <snap-xxxxxxxx>" >&2; exit 2 ;;
esac

cat <<EOF
To restore ${STACK_NAME} from ${SNAP_ID}:
  1) ./teardown.sh ${STACK_NAME}     # snapshots current data, deletes the stack
  2) edit ../config/params.env  ->  DataVolumeSnapshotId=${SNAP_ID}
  3) ./deploy.sh ${STACK_NAME}       # boots a fresh instance from the snapshot
EOF
