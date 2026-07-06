#!/bin/bash
# Stop the GPU meter. Default STOPS the instance (keeps the 120GB volume with
# the model weights — ~$9.6/mo); pass --terminate to delete everything.
#
#   ./stop.sh              # graceful server stop + EC2 stop, confirm stopped
#   ./stop.sh --terminate  # same but terminate (next launch = full setup again)
set -euo pipefail
cd "$(dirname "$0")"
source ./_common.sh
ACTION=stop
[ "${1:-}" = "--terminate" ] && ACTION=terminate

read -r IID STATE IP LAUNCHED <<<"$(find_instance)" || true
if [ -z "${IID:-}" ] || [ "$IID" = "None" ]; then
  echo "no $TAG_NAME instance in pending/running/stopped state — GPU meter is OFF ✓"
  exit 0
fi
echo "── $IID is $STATE"

if [ "$STATE" = "running" ]; then
  # Graceful first: connected pages get a clean websocket close (they fall back
  # to the static portrait / /talk on their own).
  ssm_run "$IID" "sudo systemctl stop laura-gpu" || true
fi

if [ "$ACTION" = "terminate" ]; then
  aws ec2 terminate-instances --instance-ids "$IID" --region "$REGION" >/dev/null
  echo "── terminating…"
  aws ec2 wait instance-terminated --instance-ids "$IID" --region "$REGION"
else
  aws ec2 stop-instances --instance-ids "$IID" --region "$REGION" >/dev/null
  echo "── stopping…"
  aws ec2 wait instance-stopped --instance-ids "$IID" --region "$REGION"
fi

FINAL=$(aws ec2 describe-instances --instance-ids "$IID" --region "$REGION" \
  --query 'Reservations[0].Instances[0].State.Name' --output text)
echo "confirmed: $IID is $FINAL — GPU meter OFF ✓"
if [ "$ACTION" = "stop" ]; then
  echo "(volume kept so weights survive: ~\$9.6/mo; use --terminate to delete everything)"
fi
