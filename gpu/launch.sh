#!/bin/bash
# Start the photoreal GPU box for ONE meeting/demo window; never always-on.
#
#   ./launch.sh                      # start (or create) the box, 90-min TTL
#   GPU_TTL_MINUTES=45 ./launch.sh   # shorter window
#
# What it does, in order:
#   1. find the laura-gpu instance (create from launch template if none exists)
#   2. start it and wait until running
#   3. arm the TTL dead-man switch: the box stops ITSELF after GPU_TTL_MINUTES
#      even if this laptop, the page, or the meeting crashes
#   4. start the streaming server (systemd unit from setup.sh)
#   5. health-check before declaring success, then print the stream URL
#
# Exit code is non-zero unless the stream answered /health.
set -euo pipefail
cd "$(dirname "$0")"
source ./_common.sh
TTL_MINUTES="${GPU_TTL_MINUTES:-90}"

read -r IID STATE IP LAUNCHED <<<"$(find_instance)" || true
if [ -z "${IID:-}" ] || [ "$IID" = "None" ]; then
  echo "── no $TAG_NAME instance found; launching from template '$LAUNCH_TEMPLATE'"
  IID=$(aws ec2 run-instances --region "$REGION" \
    --launch-template "LaunchTemplateName=$LAUNCH_TEMPLATE" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$TAG_NAME},{Key=project,Value=laura}]" \
    --query 'Instances[0].InstanceId' --output text)
  echo "── launched $IID (first boot: run setup.sh before the box is useful; see README)"
elif [ "$STATE" = "stopping" ]; then
  echo "── $IID is stopping; waiting before restart…"
  aws ec2 wait instance-stopped --instance-ids "$IID" --region "$REGION"
  aws ec2 start-instances --instance-ids "$IID" --region "$REGION" >/dev/null
elif [ "$STATE" = "stopped" ]; then
  echo "── starting $IID"
  aws ec2 start-instances --instance-ids "$IID" --region "$REGION" >/dev/null
else
  echo "── $IID already $STATE"
fi

aws ec2 wait instance-running --instance-ids "$IID" --region "$REGION"
IP=$(aws ec2 describe-instances --instance-ids "$IID" --region "$REGION" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "── running at $IP"

# TTL dead-man switch (issue #3): overrides the baked-in 90-min boot TTL from
# setup.sh with this window's value. shutdown -h on an EBS-backed instance = STOP.
echo "── arming ${TTL_MINUTES}-minute auto-stop"
if wait_ssm "$IID"; then
  ssm_run "$IID" "sudo shutdown -c 2>/dev/null || true; sudo shutdown -h +$TTL_MINUTES" \
    || echo "WARN: could not arm TTL over SSM - the boot-time 90-min TTL still applies"
  # Streaming server: normally auto-starts at boot; start is idempotent.
  ssm_run "$IID" "sudo systemctl start laura-gpu" \
    || echo "NOTE: laura-gpu service missing - first boot? Push code + run setup.sh (README)"
else
  echo "WARN: SSM agent never came online; boot-time 90-min TTL is the only guard"
fi

echo "── health check: http://$IP:8080/health"
for _ in $(seq 1 30); do
  if OUT=$(curl -sf --max-time 3 "http://$IP:8080/health"); then
    echo "── healthy: $OUT"
    echo
    echo "STREAM URL : ws://$IP:8080/stream"
    [ -n "${GPU_PUBLIC_STREAM_URL:-}" ] && echo "PUBLIC URL : $GPU_PUBLIC_STREAM_URL"
    echo "AUTO-STOP  : in $TTL_MINUTES min (plus idle watchdog on the box)"
    echo "COST       : \$$HOURLY_USD/hr while running. ./stop.sh the moment you're done"
    exit 0
  fi
  sleep 5
done
echo "FAILED: no healthy /health after 150s. The instance IS running and billing -" >&2
echo "        fix it (./status.sh, SSM) or shut it down now: ./stop.sh" >&2
exit 1
