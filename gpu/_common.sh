#!/bin/bash
# Shared plumbing for launch.sh / stop.sh / status.sh (run on the operator
# machine with AWS CLI creds). The GPU box is found by its Name tag so the
# scripts keep working across stop/start cycles and re-launches.

REGION="${AWS_REGION:-eu-central-1}"
TAG_NAME="${GPU_INSTANCE_NAME:-laura-gpu}"
LAUNCH_TEMPLATE="${GPU_LAUNCH_TEMPLATE:-laura-gpu}"
HOURLY_USD="${GPU_HOURLY_USD:-1.006}"   # g5.xlarge on-demand, eu-central-1

find_instance() {
  # Newest non-terminated instance tagged Name=$TAG_NAME.
  # Prints: "<instance-id> <state> <public-ip|None> <launch-time>" (or nothing).
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$TAG_NAME" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[] | sort_by(@, &LaunchTime) | [-1].[InstanceId,State.Name,PublicIpAddress,LaunchTime]' \
    --output text 2>/dev/null
}

wait_ssm() {
  # SSM agent registration can lag boot by a minute or two.
  local iid="$1"
  for _ in $(seq 1 30); do
    local st
    st=$(aws ssm describe-instance-information --region "$REGION" \
      --filters "Key=InstanceIds,Values=$iid" \
      --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)
    [ "$st" = "Online" ] && return 0
    sleep 5
  done
  return 1
}

ssm_run() {
  # Run a shell command on the box via SSM (no SSH keys on this instance).
  # Best-effort: returns non-zero on failure, never hangs past ~60s.
  local iid="$1"; shift
  local cmd_id
  cmd_id=$(aws ssm send-command --region "$REGION" --instance-ids "$iid" \
    --document-name AWS-RunShellScript \
    --parameters "commands=[\"$*\"]" \
    --query 'Command.CommandId' --output text 2>/dev/null) || return 1
  for _ in $(seq 1 20); do
    local st
    st=$(aws ssm get-command-invocation --region "$REGION" \
      --command-id "$cmd_id" --instance-id "$iid" \
      --query 'Status' --output text 2>/dev/null || echo Pending)
    case "$st" in
      Success) return 0 ;;
      Failed|Cancelled|TimedOut) return 1 ;;
    esac
    sleep 3
  done
  return 1
}
