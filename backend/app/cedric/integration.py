"""Cedric orchestrator integration logic, extracted out of ``main.py``.

Each function here is a self-contained block that ``main.py`` used to inline.
Keeping them here means the shared session-lifecycle code in ``main.py`` stays
close to upstream Laura — the fork's footprint is a handful of one-line calls
(``# CEDRIC`` markers) instead of ~200 interleaved lines.

Everything is best-effort by design: a callback failure must never block or
fail the meeting lifecycle (the artifact is always saved; the orchestrator can
poll ``GET /sessions/{bot_id}/artifact``).
"""
from __future__ import annotations

import asyncio
import hmac
import threading
from typing import Any, Optional

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import settings
from . import callback

# Meeting briefs are markdown from the orchestrator; cap so a runaway payload
# can't blow up prompts (the orchestrator summarizes down, we never truncate).
MAX_BRIEF_BYTES = 32 * 1024

# Recall bot status_code -> orchestrator session.status, for non-terminal
# join-progress relaying.
_STATUS_MAP = {
    "joining_call": "joining",
    "in_waiting_room": "joining",
    "in_call": "live",
    "in_call_not_recording": "live",
    "in_call_recording": "live",
}


class MeetingContext(BaseModel):
    """The orchestrator's per-session context payload (StartRequest.context)."""

    meeting: dict = {}          # title, starts_at, ends_at, organizer, attendees
    brief_markdown: str = ""    # the assembled pre-meeting brief


def auth_error(request: Request) -> Optional[JSONResponse]:
    """Bearer-token gate for the session API. LAURA_API_TOKEN unset = open
    (preserves the zero-key local demo); set it in any real deployment."""
    token = settings.laura_api_token.strip()
    if not token:
        return None
    provided = request.headers.get("authorization", "")
    if hmac.compare_digest(provided, f"Bearer {token}"):
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def brief_too_large(brief: str) -> Optional[JSONResponse]:
    """400 response when an injected brief exceeds the cap, else None."""
    if len(brief.encode()) > MAX_BRIEF_BYTES:
        return JSONResponse(
            {"error": f"context.brief_markdown exceeds {MAX_BRIEF_BYTES} bytes"},
            status_code=400,
        )
    return None


def build_integration(req: Any, brief: str) -> Optional[dict]:
    """Assemble the per-session integration dict from a StartRequest, or None
    when the request carries no orchestrator wiring (plain local start).

    Model A default routing: when SURFACE_WEBHOOK_URL is configured, a session
    that DIDN'T supply its own callback_url still gets one — so EVERY meeting
    (however summoned: email, calendar, API) hands its session.status /
    session.ended / action.requested to Cedric's receiver, and Cedric does the
    Slack posting + execution with his own tools. Unset = today's behaviour
    (autonomous / Model B)."""
    callback = req.callback_url or settings.surface_webhook_url
    # Pre-meeting context pull (Cedric → Laura): a default context_url means
    # every meeting fetches "who's who + context" from Cedric's memory at join,
    # so the avatar walks in already knowing the people. Explicit per-session
    # context_url still wins. Unset = only the local Drive/ledger brief.
    context_url = req.context_url or settings.surface_context_url
    if not (callback or context_url or req.external_ref or brief):
        return None
    return {
        "callback_url": callback or "",
        "context_url": context_url or "",
        "external_ref": req.external_ref or {},
        "brief": brief,
        "meeting": (req.context.meeting if req.context else {}) or {},
        "context_refreshed": False,
    }


def default_integration() -> Optional[dict]:
    """The Model A default routing for summons that DON'T go through
    ``POST /sessions/start`` — the Gmail 'Add people' auto-join watcher and the
    calendar sync webhook. Those paths never build a StartRequest, so
    ``build_integration`` (which reads the SURFACE_* defaults off the request)
    never runs for them and the meeting silently falls to Model B.

    This is the request-less equivalent: it applies the same SURFACE_WEBHOOK_URL
    / SURFACE_CONTEXT_URL defaults so EVERY meeting — however summoned — hands
    its session.status / session.ended / action.requested to Cedric's receiver
    and pulls his pre-meeting context. Returns None when NEITHER surface var is
    set (preserving the autonomous / Model B default), so a deployment with no
    orchestrator is unchanged.

    The returned dict uses the exact same keys as ``build_integration`` so every
    downstream consumer (deliver_ended, handle_webhook_status, inject_brief,
    the callback senders) works unchanged. ``external_ref`` / ``brief`` /
    ``meeting`` start empty because there is no request to carry them; the fresh
    brief is pulled at join via ``handle_webhook_status`` -> ``fetch_context``
    when context_url is set."""
    callback = settings.surface_webhook_url
    context_url = settings.surface_context_url
    if not (callback or context_url):
        return None
    return {
        "callback_url": callback or "",
        "context_url": context_url or "",
        "external_ref": {},
        "brief": "",
        "meeting": {},
        "context_refreshed": False,
    }


# Transcripts are PII: they live in the local artifact store (served only by
# the local /meetings archive) and NEVER cross the orchestrator API — webhooks
# and the session endpoints get the distilled artifact. The version marker lets
# clients parse additively as fields are added.
ARTIFACT_VERSION = 1


def wire_artifact(artifact: dict) -> dict:
    """The orchestrator-facing copy of an artifact: distilled fields only, no
    raw transcript, stamped with ``artifact_version``."""
    wire = {k: v for k, v in artifact.items() if k != "transcript"}
    wire["artifact_version"] = ARTIFACT_VERSION
    return wire


def deliver_ended(integration: Optional[dict], bot_id: str, artifact: dict) -> bool:
    """Hand the finished artifact to the orchestrator's webhook (retried inside
    send_ended). Fire-and-forget — the artifact is already saved and the
    orchestrator polls as a fallback. Returns True when this is an orchestrated
    session (so the caller skips autopilot delivery — the orchestrator owns
    approval-gated email + Slack for its sessions)."""
    if integration and integration.get("callback_url"):
        asyncio.create_task(
            run_in_threadpool(
                callback.send_ended, integration, bot_id, wire_artifact(artifact)
            )
        )
        return True
    return False


def notify_action_requested(session: Any, bot_id: str, item: dict) -> None:
    """An action request was captured live (tools.queue_action): tell the
    orchestrator NOW, so the Slack approval card is ready before the meeting
    ends. No-ops unless the session is orchestrated (integration with a
    callback_url). Fire-and-forget and OFF the live path — the artifact's
    actions[] at finalize stays the authoritative copy, so a lost event costs
    nothing. PII rule: only the distilled action/owner/due (plus the stable,
    non-PII action_id used to correlate this event with the final artifact)
    leave — never transcript content."""
    if session is None or not session.integration:
        return
    integration = dict(session.integration)
    if not integration.get("callback_url"):
        return
    wire_item = {
        k: (item or {}).get(k, "") for k in ("action_id", "action", "owner", "due")
    }
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Tool dispatch runs inside run_in_threadpool — no event loop in this
        # worker thread, so create_task would raise. A daemon thread keeps the
        # POST just as fire-and-forget and off the live path.
        threading.Thread(
            target=callback.send_action_requested,
            args=(integration, bot_id, wire_item),
            daemon=True,
        ).start()
        return
    asyncio.create_task(
        run_in_threadpool(callback.send_action_requested, integration, bot_id, wire_item)
    )


def notify_failed(session: Any, bot_id: str, status_code: str) -> None:
    """A fatal bot status never reached 'live': tell the orchestrator the join
    failed (best-effort). Finalize still runs for cleanup."""
    if session is not None and session.integration and status_code == "fatal":
        asyncio.create_task(
            run_in_threadpool(
                callback.send_status,
                dict(session.integration),
                bot_id,
                "failed",
                status_code,
            )
        )


async def handle_webhook_status(session: Any, bot_id: str, status_code: str) -> None:
    """Non-terminal bot status: relay join progress to the orchestrator and
    refresh the meeting brief once the bot is actually in the call (a booking
    made days ago has a stale brief by now). Both best-effort."""
    if session is None or not session.integration or not status_code:
        return
    mapped = _STATUS_MAP.get(status_code)
    if mapped:
        asyncio.create_task(
            run_in_threadpool(
                callback.send_status,
                dict(session.integration),
                bot_id,
                mapped,
                status_code,
            )
        )
    if mapped == "live" and not session.integration.get("context_refreshed"):
        integration = dict(session.integration)
        integration["context_refreshed"] = True
        session.integration = integration  # persist: refresh runs once
        fresh = await run_in_threadpool(callback.fetch_context, integration)
        if fresh and isinstance(fresh.get("brief_markdown"), str):
            integration = dict(integration)
            integration["brief"] = fresh["brief_markdown"]
            if isinstance(fresh.get("meeting"), dict):
                integration["meeting"] = fresh["meeting"]
            session.integration = integration


def inject_brief(session: Any, memory: str) -> str:
    """Fold the orchestrator's meeting brief (agenda, participants, open items)
    into the live-prompt memory channel, ahead of the cross-meeting carryover —
    it's the most specific context this session has."""
    brief = (session.integration or {}).get("brief", "")
    if brief:
        return (
            f"MEETING BRIEF (from the orchestrator):\n{brief}\n\n{memory}".strip()
        )
    return memory
