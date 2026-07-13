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
import json
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

# Strong refs to in-flight fire-and-forget ``session.ended`` sends. asyncio only
# holds a WEAK reference to a bare create_task, so a running task can be GC'd
# mid-flight (Python docs) — dropping the delivery. /redeliver now makes this
# path directly (and repeatably) HTTP-triggerable, so keep each task alive until
# it finishes, then drop it. Same pattern as main.py's _summary_tasks.
_ended_tasks: set = set()

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


def resolve_machine_org(request: Request) -> Optional[str]:
    """The org this request's MACHINE bearer is scoped to, or None when the
    request carries no recognized machine credential.

      - The GLOBAL ``laura_api_token`` (when configured) → ``settings.
        demo_org_id``: the legacy service scope. Endpoints treat it exactly as
        today (Cedric's deployment credential, never narrowed by this PR).
      - A PER-ORG token (org_tokens — durable control plane first, SQLite
        fallback; PR A) → its org_id. Callers MUST enforce that such a bearer
        only touches its own org's rows.
      - Anything else (no/blank/unknown bearer) → None; callers fall back to
        ``auth_error`` / ``auth.gate`` so the key-free demo stays
        byte-identical (open when no token is configured).

    Sibling of ``main._org_token_bearer_org`` (PR A), which returns None for
    the global bearer instead — deliberately NOT consolidated so PR A's
    start/end/redeliver semantics stay untouched.

    SYNC (SQLite + optionally a Postgres round-trip): async handlers must call
    it via ``run_in_threadpool`` — never on the live hot path. The raw secret
    is compared/hashed only, never logged."""
    provided = request.headers.get("authorization", "")
    if not provided.startswith("Bearer "):
        return None
    raw = provided[len("Bearer "):].strip()
    if not raw:
        return None
    global_token = settings.laura_api_token.strip()
    if global_token and hmac.compare_digest(raw, global_token):
        return settings.demo_org_id
    # Lazy imports keep the module graph flat (cedric never needs store/
    # control_plane at import time; mirrors auth.py's local `import cedric`).
    from .. import control_plane, store

    org = control_plane.resolve_org_token(raw)
    if org is None:
        org = store.resolve_org_token(raw)
    return org


def brief_too_large(brief: str) -> Optional[JSONResponse]:
    """400 response when an injected brief exceeds the cap, else None."""
    if len(brief.encode()) > MAX_BRIEF_BYTES:
        return JSONResponse(
            {"error": f"context.brief_markdown exceeds {MAX_BRIEF_BYTES} bytes"},
            status_code=400,
        )
    return None


def default_external_ref() -> dict:
    """SURFACE_EXTERNAL_REF (JSON, e.g. '{"team":"T1","slack_channel":"#cedric",
    "requested_by":"duccio"}') parsed into the external_ref every session falls
    back to. Live test 2026-07-10: sessions summoned by email/dashboard reached
    the surface with external_ref {} — the orchestrator had no Slack channel to
    route to, so cards and recaps were silently dropped. A configured default
    makes EVERY meeting routable; a session that carries its own external_ref
    (a real Cedric summon) still wins. Unset/invalid JSON → {} (old behaviour)."""
    raw = (settings.surface_external_ref or "").strip()
    if not raw:
        return {}
    try:
        ref = json.loads(raw)
        return ref if isinstance(ref, dict) else {}
    except ValueError:
        print(
            "[cedric] SURFACE_EXTERNAL_REF is not valid JSON — ignoring it",
            flush=True,
        )
        return {}


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
        "external_ref": req.external_ref or default_external_ref(),
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
    brief is pulled at join via ``maybe_refresh_context`` -> ``fetch_context``
    (triggered by the first "live" status OR the first transcript webhook)
    when context_url is set."""
    callback = settings.surface_webhook_url
    context_url = settings.surface_context_url
    if not (callback or context_url):
        return None
    return {
        "callback_url": callback or "",
        "context_url": context_url or "",
        "external_ref": default_external_ref(),
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
        task = asyncio.create_task(
            run_in_threadpool(
                callback.send_ended, integration, bot_id, wire_artifact(artifact)
            )
        )
        # Hold a strong ref until the send finishes (asyncio only refs it weakly).
        _ended_tasks.add(task)
        task.add_done_callback(_ended_tasks.discard)
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
    # PII-safe telemetry (action_id only, never the action text): the live
    # "Cedric said he could but did nothing" report is undiagnosable without
    # knowing whether the captured action even had a surface to go to. Two
    # distinct no-op reasons, so a mis-summoned (non-orchestrated) session is
    # told apart from a genuine send.
    aid = (item or {}).get("action_id", "")
    if session is None or not session.integration:
        print(
            f"[cedric] action captured (action_id={aid!r}) but session is NOT "
            "orchestrated (no integration) — nothing sent to the surface",
            flush=True,
        )
        return
    integration = dict(session.integration)
    if not integration.get("callback_url"):
        print(
            f"[cedric] action captured (action_id={aid!r}) but session has no "
            "callback_url — nothing sent to the surface",
            flush=True,
        )
        return
    print(
        f"[cedric] action.requested dispatching to surface (action_id={aid!r})",
        flush=True,
    )
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
    if mapped == "live":
        maybe_refresh_context(session)


def maybe_refresh_context(session: Any) -> bool:
    """One-shot pre-meeting context pull (Cedric -> Laura), shared by BOTH
    triggers:

      - ``handle_webhook_status`` when a Recall status maps to "live" — the
        original trigger, which production (2026-07-10) shows often never
        fires: the realtime webhook delivers no bot-status events at all
        (every finalize that day was source=reconcile), so the avatar sat in
        meetings without Cedric's brief; and
      - the FIRST transcript webhook of the session (``main.py``, partial or
        final) — the fallback: a transcript is proof the bot is in the call.

    Fire-and-forget and OFF the live path: this function only does dict
    checks; the GET runs in the threadpool inside a task. ``context_refreshed``
    is flipped (and persisted) BEFORE the task launches so the refresh runs
    once per session — and since there is no await between check and flip,
    racing partial/final webhooks on the same event loop cannot double-launch.
    Returns True when a refresh task was launched."""
    if session is None or not session.integration:
        return False  # not an orchestrated session
    integration = session.integration
    if not integration.get("context_url") or integration.get("context_refreshed"):
        return False
    integration = dict(integration)
    integration["context_refreshed"] = True
    session.integration = integration  # persist first: refresh runs once
    asyncio.create_task(_refresh_context(session, integration))
    return True


async def _refresh_context(session: Any, integration: dict) -> None:
    """The refresh body behind ``maybe_refresh_context``: GET the fresh
    context off the event loop and fold it into the session. Best-effort —
    ``fetch_context`` swallows transport errors (returns None) and a
    malformed payload just keeps the booking-time brief."""
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
