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
import time
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
    # Optional per-meeting MISSION (admin objective for THIS call): an aim the
    # avatar keeps in mind and RESURFACES if left unmet ("on an investor call, if
    # they haven't covered market size, raise it"). Overrides the avatar's default
    # mission (avatar.yaml). "" = no per-meeting mission = the avatar default (or,
    # if that too is empty, today's behaviour exactly).
    mission: str = ""


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
    # A per-meeting MISSION alone is enough to build a session integration: a
    # dashboard dispatch that sets only a mission (no callback/context_url/
    # external_ref/brief) must still carry it, else resolve_mission() finds
    # nothing and the objective is silently dropped. Empty mission = unchanged.
    mission = (req.context.mission if req.context else "") or ""
    if not (callback or context_url or req.external_ref or brief or mission):
        return None
    return {
        "callback_url": callback or "",
        "context_url": context_url or "",
        "external_ref": req.external_ref or default_external_ref(),
        "brief": brief,
        "meeting": (req.context.meeting if req.context else {}) or {},
        # Per-meeting mission (admin objective) carried on the session, if any.
        "mission": mission,
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
        "mission": "",
        "context_refreshed": False,
    }


# Transcripts are PII: they live in the local artifact store (served only by
# the local /meetings archive) and NEVER cross the orchestrator API — webhooks
# and the session endpoints get the distilled artifact. The version marker lets
# clients parse additively as fields are added.
# v2 adds the additive ``decision_records`` field (first-class decisions:
# maker/reason/related_project/supersede link) alongside the unchanged
# ``decisions`` list[str]. Old clients keep reading ``decisions``.
ARTIFACT_VERSION = 2


_WIRE_ARTIFACT_KEYS = {
    "summary",
    "decisions",
    "decision_records",
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
    """LIVE context pull (Cedric -> Laura), shared by BOTH triggers:

      - ``handle_webhook_status`` when a Recall status maps to "live" — the
        original trigger, which production (2026-07-10) shows often never
        fires: the realtime webhook delivers no bot-status events at all
        (every finalize that day was source=reconcile), so the avatar sat in
        meetings without Cedric's brief; and
      - EVERY transcript webhook of the session (``main.py``, partial or
        final) — a transcript is proof the bot is in the call, and repeated
        transcripts make the pull PERIODIC: while people are talking, the
        brief is re-pulled whenever the last pull is older than
        CONTEXT_REFRESH_SECONDS, so the avatar speaks from the current state
        of Asana/Slack for the whole meeting instead of a join-time snapshot.
        (0 restores the old one-shot behaviour. A quiet meeting stops
        pulling — no transcript, no refresh, no load.)

    Fire-and-forget and OFF the live path: this function only does dict/clock
    checks; the GET runs in the threadpool inside a task. The timestamp is
    stamped (and persisted) BEFORE the task launches — and since there is no
    await between check and stamp, racing partial/final webhooks on the same
    event loop cannot double-launch. Returns True when a refresh launched."""
    if session is None or not session.integration:
        return False  # not an orchestrated session
    integration = session.integration
    if not integration.get("context_url"):
        return False
    last = float(integration.get("context_refreshed_at") or 0.0)
    if integration.get("context_refreshed") and not last:
        # Legacy one-shot flag from a pre-upgrade session (mid-meeting deploy):
        # treat the join-time pull as "just now" so periodic takes over cleanly.
        last = time.time()
    if last:
        interval = settings.context_refresh_seconds
        if interval <= 0 or time.time() - last < interval:
            return False
    integration = dict(integration)
    integration["context_refreshed"] = True
    integration["context_refreshed_at"] = time.time()
    session.integration = integration  # persist first: one launch per window
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


def voice_approve(session: Any, item: dict) -> bool:
    """Voice consent: an ADDRESSED mid-meeting ask ("Petra, create a task…")
    IS the approval. Records the ONE canonical decision (decided_via='voice' —
    same convergence rules as dashboard/Slack/chat: first write wins, replays
    refused) and fires ``action.approved`` on the per-org events door so
    Cedric executes NOW instead of parking a card until after the call.

    Only the deterministic addressed-capture path calls this (main.py):
    actions the summarizer merely INFERS at finalize never come here — they
    keep the human click. Best-effort and off the live path (the spoken
    confirmation has already been said); a delivery miss leaves the action
    'approved' and visible on the dashboard, where Retry semantics apply."""
    aid = str((item or {}).get("action_id") or "").strip()
    if not aid:
        return False
    from .. import ledger, store  # lazy: keeps the module graph flat

    org = str(
        (session.integration or {}).get("org_id")
        or getattr(session, "org_id", "")
        or settings.demo_org_id
    )
    recorded = store.record_action_approval(
        org, aid, decision="approve", decided_via="voice",
        previous_status="requested", new_status="approved",
    )
    if not recorded:
        # A canonical decision already exists (replay, or another surface got
        # there first — first write wins). Never double-fire the execute
        # signal on top of someone else's decision.
        return False
    ledger.set_action_status(
        aid, "approved", "voice-approved in the meeting", org_id=org
    )
    return callback.send_action_event(
        org,
        "action.approved",
        {
            "action_id": aid,
            "bot_id": str(getattr(session, "bot_id", "") or ""),
            "decided_via": "voice",
            "action": str((item or {}).get("action") or "")[:300],
            "owner": str((item or {}).get("owner") or "")[:100],
            "due": str((item or {}).get("due") or "")[:100],
        },
    )


class ContextPush(BaseModel):
    """Body of POST /sessions/{bot_id}/context — a LIVE push (Cedric → Laura)."""

    context: MeetingContext


def apply_context_push(
    session: Any, req: ContextPush, caller_org: Optional[str]
) -> JSONResponse:
    """Real-time counterpart of the periodic context pull: the orchestrator
    PUSHES a fresh brief the moment something material changes (a task closed,
    a decision landed in Slack) instead of waiting for the next pull window.

    Same payload shape as ``StartRequest.context`` / ``fetch_context``. Each
    push REPLACES the stored brief (re-summarize upstream — never an append
    log; the byte cap stays authoritative), and ``inject_brief`` re-reads the
    stored brief every turn, so the avatar's next answer already speaks from
    the pushed state. A push also resets the pull window — pushing
    orchestrators aren't double-polled.

    Scope: a PER-ORG machine bearer may only push into its own org's sessions;
    the global bearer / key-free demo keeps legacy scope. Unknown-or-foreign
    sessions answer the same 404 (existence never leaks across tenants)."""
    if session is None:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    session_org = str(
        (session.integration or {}).get("org_id")
        or getattr(session, "org_id", "")
        or ""
    )
    if caller_org and caller_org != settings.demo_org_id and session_org != caller_org:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    brief = req.context.brief_markdown or ""
    if err := brief_too_large(brief):
        return err
    if not brief and not req.context.meeting and not req.context.mission:
        return JSONResponse({"error": "empty context push"}, status_code=400)
    integration = dict(session.integration or {})
    if brief:
        integration["brief"] = brief
    if req.context.meeting:
        integration["meeting"] = req.context.meeting
    if req.context.mission:
        integration["mission"] = req.context.mission
    integration["context_refreshed"] = True
    integration["context_refreshed_at"] = time.time()
    session.integration = integration  # assignment persists (store.Session)
    return JSONResponse({"ok": True, "brief_bytes": len(brief.encode())})


def _cedric_brief_allowed(session: Any) -> bool:
    """Whether THIS session may carry the orchestrator's Cedric-branded brief.

    The brief opens with "You are Cedric's presence in this meeting" and
    advertises Cedric's tool fleet — injected into a Petra/Laura session it
    hijacks both identity and capabilities (live 2026-07-21: Petra introduced
    Cedric's 3,000-app roster in a meeting whose org had no Slack agent at
    all, because SURFACE_CONTEXT_URL is a global Model-A default). Allowed
    when the acting avatar IS cedric, or the org explicitly connected the
    cedric-brain on the dashboard."""
    if str(getattr(session, "avatar_id", "") or "").strip().lower() == "cedric":
        return True
    try:
        from .. import store

        rows = store.connections_for_org(
            str(getattr(session, "org_id", "") or "")
        )
        return any(
            r.get("provider") == "cedric-brain"
            and r.get("status") == "connected"
            for r in rows
        )
    except Exception:  # noqa: BLE001 — no store, no claim
        return False


def inject_brief(session: Any, memory: str) -> str:
    """Fold the orchestrator's meeting brief (agenda, participants, open items)
    into the live-prompt memory channel, ahead of the cross-meeting carryover —
    it's the most specific context this session has. Cedric-branded content is
    identity-gated (see _cedric_brief_allowed)."""
    brief = (session.integration or {}).get("brief", "")
    if brief and _cedric_brief_allowed(session):
        return (
            f"MEETING BRIEF (from the orchestrator):\n{brief}\n\n{memory}".strip()
        )
    return memory


def resolve_mission(session: Any) -> str:
    """The per-meeting mission set on THIS session (MeetingContext.mission,
    carried on ``integration``), or "" when none was set. The caller falls back
    to the avatar's default mission (avatar.yaml) — so a per-session mission
    wins, an avatar default applies otherwise, and neither means today's
    behaviour exactly."""
    return ((session.integration or {}).get("mission", "") if session else "") or ""
