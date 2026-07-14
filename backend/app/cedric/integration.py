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
import ipaddress
import json
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import settings
from .. import outbox
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

    # In production the durable control plane is authoritative. Falling back
    # to SQLite after a durable miss would resurrect a token revoked in
    # Postgres. SQLite is only the key-free/local control plane.
    if control_plane.enabled():
        return control_plane.resolve_org_token(raw)
    return store.resolve_org_token(raw)


def provisioning_auth_ok(request: Request) -> bool:
    """True only for the dedicated Laura↔Cedric OAuth bootstrap credential.

    This credential is accepted solely by Slack install completion. It is not
    a session/archive bearer and therefore cannot become a cross-tenant master
    key. Empty is always disabled.
    """
    token = settings.cedric_orgs_token.strip()
    if not token:
        return False
    provided = request.headers.get("authorization", "")
    return hmac.compare_digest(provided, f"Bearer {token}")


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """Return a safe public HTTPS origin, otherwise None.

    Credentials are attached to these requests, so userinfo, custom ports and
    local/private/link-local/reserved literal IPs are never valid integration
    destinations even when an operator accidentally configures one.
    """
    try:
        p = urlsplit((url or "").strip())
        port = p.port
    except ValueError:
        return None
    host = (p.hostname or "").lower().rstrip(".")
    if (
        p.scheme.lower() != "https"
        or not host
        or p.username is not None
        or p.password is not None
        or port not in (None, 443)
        or host == "localhost"
        or host.endswith(".localhost")
    ):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass  # DNS name; exact-origin allowlisting below remains authoritative.
    else:
        if not address.is_global:
            return None
    return ("https", host, port)


def request_integration_urls_allowed(req: Any, org_id: str) -> bool:
    """Reject customer-supplied callback/context endpoints outside Cedric.

    Per-org callbacks carry workspace credentials. They may only go to an HTTPS
    origin configured by the operator; Demo/key-free traffic keeps its legacy
    behavior. Server-owned default URLs are already trusted configuration.
    """
    org = (org_id or "").strip()
    if not org or org == settings.demo_org_id:
        return True
    supplied = [
        str(getattr(req, "callback_url", "") or "").strip(),
        str(getattr(req, "context_url", "") or "").strip(),
    ]
    supplied = [url for url in supplied if url]
    if not supplied:
        return True
    trusted = {
        origin
        for origin in (
            _origin(settings.cedric_orgs_url),
            _origin(settings.surface_webhook_url),
            _origin(settings.surface_context_url),
        )
        if origin is not None
    }
    return bool(trusted) and all(_origin(url) in trusted for url in supplied)


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


_WIRE_ARTIFACT_KEYS = {
    "summary",
    "decisions",
    "actions",
    "checklist",
    "missing_steps",
    "readiness_score",
    "risks",
    "follow_up_email",
    "avatar_id",
    "org_id",
    "duration_seconds",
}


def wire_artifact(artifact: dict) -> dict:
    """Distilled orchestrator copy, built from an allowlist.

    A negative transcript filter is not future-safe: a later archive field
    such as raw_transcript/utterances/segments could silently enter the durable
    Postgres payload. Only the reviewed product artifact crosses this boundary;
    the meeting URL is already represented by the callback routing envelope.
    """
    wire = {
        key: artifact[key]
        for key in _WIRE_ARTIFACT_KEYS
        if key in artifact
    }
    wire["artifact_version"] = ARTIFACT_VERSION
    return wire


def _kick_outbox() -> None:
    """Nudge delivery off-path; the lifespan worker remains the crash backstop.

    When called from a worker thread there is no running event loop. In that
    case the durable row is already committed and the lifespan worker will
    deliver it; starting a detached thread here can race process shutdown (and
    tests that swap SQLite files), which defeats the durability guarantee.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.create_task(run_in_threadpool(outbox.process_due))


def deliver_ended(integration: Optional[dict], bot_id: str, artifact: dict) -> bool:
    """Durably enqueue session.ended; network delivery never blocks finalize."""
    if integration and integration.get("callback_url"):
        wire = wire_artifact(artifact)
        canonical = outbox.checkpoint_session_ended(
            dict(integration), bot_id, wire
        )
        # The first durable wire envelope is canonical after a crash/retry.
        # Replace only distilled fields; local transcript/meeting URL stay local.
        for key in _WIRE_ARTIFACT_KEYS | {"artifact_version"}:
            artifact.pop(key, None)
        artifact.update(canonical)
        _kick_outbox()
        return True
    return False

def notify_action_requested(session: Any, bot_id: str, item: dict) -> None:
    """Nudge delivery after persist_action_capture committed atomically."""
    aid = str((item or {}).get("action_id") or "")
    if session is None or not session.integration:
        print(
            f"[cedric] action captured (action_id={aid!r}) but session is NOT "
            "orchestrated (no integration)",
            flush=True,
        )
        return
    if not session.integration.get("callback_url"):
        return
    _kick_outbox()

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


def _with_live_meeting(session: Any, integration: dict) -> dict:
    """A copy of ``integration`` whose ``meeting`` carries THIS call's identity —
    the live Recall roster (everyone who's joined, incl. non-speakers) as
    ``attendees`` and any known title — so ``fetch_context`` asks Cedric for a
    MEETING-SPECIFIC brief instead of a workspace-only one. Best-effort: a
    roster read failure or an already-populated booking meeting is left as-is."""
    meeting = dict(integration.get("meeting") or {})
    try:
        roster = session.roster()
    except Exception:  # noqa: BLE001 — enrichment must never break the pull
        roster = []
    if roster and not meeting.get("attendees"):
        meeting["attendees"] = [{"name": name} for name in roster]
    out = dict(integration)
    out["meeting"] = meeting
    return out


async def _refresh_context(session: Any, integration: dict) -> None:
    """The refresh body behind ``maybe_refresh_context``: GET the fresh
    context off the event loop and fold it into the session. Best-effort —
    ``fetch_context`` swallows transport errors (returns None) and a
    malformed payload just keeps the booking-time brief."""
    outbound = _with_live_meeting(session, integration)
    fresh = await run_in_threadpool(callback.fetch_context, outbound)
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
