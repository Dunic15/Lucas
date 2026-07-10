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
  - default_integration — SURFACE_* default routing for non-API summons
  - brief_too_large     — request-validation helper
  - deliver_ended       — hand the finished artifact to the orchestrator
  - wire_artifact       — orchestrator-facing artifact copy (no transcript)
  - handle_webhook_status / notify_failed — relay Recall bot status
  - maybe_refresh_context — one-shot pre-meeting context pull (status OR
                            first-transcript trigger)
  - notify_action_requested — fire action.requested when queue_action captures
  - inject_brief        — fold the meeting brief into the live prompt memory
  - provision_org       — register an org→workspace link (Connect the brain)
"""
from __future__ import annotations

from . import callback  # noqa: F401
from .callback import provision_org  # noqa: F401
from .integration import (  # noqa: F401
    ARTIFACT_VERSION,
    MAX_BRIEF_BYTES,
    MeetingContext,
    auth_error,
    brief_too_large,
    build_integration,
    default_integration,
    deliver_ended,
    handle_webhook_status,
    inject_brief,
    maybe_refresh_context,
    notify_action_requested,
    notify_failed,
    wire_artifact,
)
