"""Meeting-bound GPU runtime (issue #3): the photoreal GPU box runs ONLY while
a meeting needs it.

Active when GPU_INSTANCE_ID is set AND avatar_page == "photoreal":
  - a session starts  -> start the EC2 instance (fire-and-forget; the photoreal
    page shows the static portrait until the stream comes up, then upgrades)
  - the last session ends -> stop the instance after gpu_idle_stop_minutes of
    grace (cancelled if a new meeting starts in the window)

Every AWS call is best-effort in a daemon thread: a boto3 failure can never
block or break the live-meeting path. And this module is only ONE of three
cost guards; the box also stops itself via its boot TTL (90 min) and its own
idle watchdog (gpu/server.py), so a crashed backend can't leave it running.

Requires ec2:StartInstances/StopInstances on the App Runner instance role,
scoped to the laura-gpu box: see gpu/iam-backend-gpu-policy.json.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from ..config import settings

try:  # optional dependency; only exercised when GPU_INSTANCE_ID is set
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None

_lock = threading.Lock()
_stop_timer: Optional[threading.Timer] = None
_count_active: Callable[[], int] = lambda: 0


def configure(count_active: Callable[[], int]) -> None:
    """Wire in a live-session counter so a scheduled stop can re-check that no
    new meeting started while it waited."""
    global _count_active
    _count_active = count_active


def enabled() -> bool:
    return bool(
        settings.gpu_instance_id
        and settings.avatar_page == "photoreal"
        and boto3 is not None
    )


def _ec2():
    return boto3.client("ec2", region_name=settings.gpu_aws_region)


def on_session_started() -> None:
    """Called when a bot is sent into a meeting. Never blocks the join path."""
    if not enabled():
        return
    global _stop_timer
    with _lock:
        if _stop_timer is not None:
            _stop_timer.cancel()
            _stop_timer = None
    threading.Thread(target=_start_instance, daemon=True).start()


def _start_instance() -> None:
    try:
        _ec2().start_instances(InstanceIds=[settings.gpu_instance_id])
        print(f"[gpu] start requested: {settings.gpu_instance_id}", flush=True)
    except Exception as e:  # noqa: BLE001; infra-only, never surfaces to the meeting
        print(f"[gpu] start failed ({type(e).__name__}) — page stays on fallback",
              flush=True)


def on_session_ended(active_count: int) -> None:
    """Called after a session is finalized. Stops the box once nothing needs it."""
    if not enabled() or active_count > 0:
        return
    global _stop_timer
    with _lock:
        if _stop_timer is not None:
            _stop_timer.cancel()
        _stop_timer = threading.Timer(
            settings.gpu_idle_stop_minutes * 60, _stop_if_still_idle
        )
        _stop_timer.daemon = True
        _stop_timer.start()
    print(f"[gpu] no active sessions — stop scheduled in "
          f"{settings.gpu_idle_stop_minutes} min", flush=True)


def _stop_if_still_idle() -> None:
    try:
        if _count_active() > 0:
            print("[gpu] stop cancelled — a new session is active", flush=True)
            return
        _ec2().stop_instances(InstanceIds=[settings.gpu_instance_id])
        print(f"[gpu] stop requested: {settings.gpu_instance_id}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[gpu] stop failed ({type(e).__name__}) — box TTL/idle watchdog "
              f"will stop it", flush=True)
