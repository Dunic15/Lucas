"""Cedric X Laura fork glue.

Everything specific to the **Cedric orchestrator integration** (the Slack agent
that books avatars into meetings, receives the distilled artifact, and drives
approvals) lives in this package, so upstream `Dunic15/Laura` merges stay clean.
`main.py` touches Cedric only at a few one-line hook points — grep the codebase
for ``# CEDRIC`` to find every seam.

Public surface:
  - callback            — signed session.status / session.ended / context calls
  - MeetingContext      — the orchestrator's per-session context payload
  - MAX_BRIEF_BYTES     — cap on an injected brief
  - auth_error          — Bearer gate for the session API
  - build_integration   — assemble the per-session integration dict
  - brief_too_large     — request-validation helper
  - deliver_ended       — hand the finished artifact to the orchestrator
  - wire_artifact       — orchestrator-facing artifact copy (no transcript)
  - handle_webhook_status / notify_failed — relay Recall bot status
  - notify_action_requested — fire action.requested when queue_action captures
  - inject_brief        — fold the meeting brief into the live prompt memory
"""
from __future__ import annotations

from . import callback  # noqa: F401
from .integration import (  # noqa: F401
    ARTIFACT_VERSION,
    MAX_BRIEF_BYTES,
    MeetingContext,
    auth_error,
    brief_too_large,
    build_integration,
    deliver_ended,
    handle_webhook_status,
    inject_brief,
    notify_action_requested,
    notify_failed,
    wire_artifact,
)
