#!/bin/bash
# One-glance answer to: "is the GPU box burning money right now?"
# Shows instance state, uptime this boot, estimated cost, and stream health.
set -euo pipefail
cd "$(dirname "$0")"
source ./_common.sh

read -r IID STATE IP LAUNCHED <<<"$(find_instance)" || true
if [ -z "${IID:-}" ] || [ "$IID" = "None" ]; then
  echo "instance : none; nothing running, nothing billing ✓"
  exit 0
fi

echo "instance : $IID ($TAG_NAME, $REGION)"
echo "state    : $STATE"
if [ "$STATE" != "running" ]; then
  echo "cost     : \$0/hr (volume ~\$9.6/mo while the instance exists)"
  exit 0
fi

# EC2 refreshes LaunchTime on every stop->start, so this is uptime of THIS boot
#; which is exactly the window being billed.
UP_MIN=$(python3 -c "
import sys, datetime
t = datetime.datetime.fromisoformat(sys.argv[1].replace('Z', '+00:00'))
print(int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() // 60))
" "$LAUNCHED")
COST=$(python3 -c "print(f'{$UP_MIN / 60 * $HOURLY_USD:.2f}')")

echo "uptime   : ${UP_MIN} min this boot"
echo "cost     : ~\$${COST} this session (\$$HOURLY_USD/hr. ./stop.sh ends it)"
echo "health   : $(curl -sf --max-time 3 "http://$IP:8080/health" || echo 'UNREACHABLE (server down or SG/port 8080)')"
echo "metrics  : $(curl -sf --max-time 3 "http://$IP:8080/metrics" || echo 'n/a')"
