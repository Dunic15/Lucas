#!/usr/bin/env bash
# Destroy the development gateway — DEVELOPMENT ONLY.
# Refuses if any target names customer infrastructure. Takes a final backup
# first, because "teardown" should never be the same thing as "data loss".
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
python3 "${HERE}/guards.py" "${TARGET_SERVICE:-laura-backend-next}" "${GATEWAY_DOMAIN:-}" \
  || { echo "[teardown] REFUSED by development-only guard"; exit 2; }
if [ "${SKIP_BACKUP:-0}" != "1" ]; then
  echo "[teardown] taking a final backup"; "${HERE}/bin/backup.sh" || true
fi
echo "[teardown] stopping and removing containers + volumes"
docker compose down -v
systemctl disable --now openclaw-gateway.service 2>/dev/null || true
rm -f /etc/systemd/system/openclaw-gateway.service /opt/openclaw/secrets.env
systemctl daemon-reload 2>/dev/null || true
echo "[teardown] local resources removed."
echo "[teardown] AWS resources (EC2 instance, EBS volume, EIP, SSM params)"
echo "           are NOT deleted by this script — remove them deliberately:"
echo "  aws ec2 terminate-instances --instance-ids <id>"
echo "  aws ec2 delete-volume --volume-id <vol>"
echo "  aws ec2 release-address --allocation-id <eipalloc>"
echo "  aws ssm delete-parameters --names /laura/next/openclaw/{ANTHROPIC_API_KEY,OPENCLAW_GATEWAY_TOKEN}"
