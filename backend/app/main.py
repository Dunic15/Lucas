"""Callable AI Process Avatar — backend.

Flow:
  POST /sessions/start  -> create Anam avatar (ElevenLabs voice) + send Recall
                           bot into the meeting rendering our avatar page.
  Recall  --transcript.data-->  POST /webhooks/recall
                           -> store utterance, run the when-to-speak gate.
                           -> if the avatar is called & confident, push the
                              answer over the websocket to the avatar page,
                              which makes the avatar speak it (Anam echo/talk).
  POST /sessions/{id}/end -> remove bot, end avatar, return post-meeting
                             summary + gap checklist + draft follow-up email.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import random
import re
import time
import traceback
import uuid
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from starlette.concurrency import iterate_in_threadpool
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

from . import (
    avatars,
    store,
    recall_client,
    anam_client,
    asana_client,
    auth,
    billing,
    cedric,
    control_plane,
    dashboard,
    drive_client,
    entitlements,
    emotion,
    end_of_turn,
    executor,
    gemini_ears,
    google_client,
    granola_client,
    actions,
    gmail_watcher,
    knowledge,
    llm,
    autopilot,
    gpu_runtime,
    runpod_runtime,
    tool_registry,
    ledger,
    outbox,
    meeting_state,
    org_api,
    security,
    tools,
    tts,
    vendor_health,
)
from .brain.engine import (
    SEARCH_ANNOUNCE_LINES,
    answer_question,
    answer_question_stream,
    answer_with_tools,
    rolling_summary,
    sounds_italian,
    wants_action_capture,
    wants_deep_thought,
    wants_web_search,
    post_meeting,
    degraded_post_meeting,
    proactive_flag,
    effective_provider,
    semantic_action_duplicates,
    type_actions,
)
from .config import settings
from .decision import (
    addressed_to_other,
    adaptive_deference_seconds,
    closing_fallback_fires,
    detect_wake,
    detect_closing,
    detect_invite,
    detect_leave_command,
    detect_leave_command_explicit,
    detect_stop_command,
    in_locked_dyad,
    interjection_floor_open,
    is_capture_continuation,
    plausible_leave_followup,
    should_interject,
    should_raise_hand,
    similar_contribution,
)
from .rag import ensure_about_index, ensure_index, warm as warm_index

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup/shutdown for the app (replaces the deprecated @app.on_event hooks).

    Startup: prebuild+warm each avatar's RAG index (so the first live question
    skips the cold start), start the autopilot nudge loop (if enabled), launch
    the Gmail 'Add people' auto-join watcher, and start the session-reconciliation
    loop (finalizes bots whose terminal status webhook never arrived). Shutdown:
    on a deploy/rollout App
    Runner SIGTERMs the old instance; flipping the drain flag stops its Gmail
    watcher from dispatching at once, so the old + new instances don't both put a
    bot in the same meeting during the overlap.
    """
    global _shutting_down
    _shutting_down = False

    # Production must prove the exact policy-bound runtime credential before
    # warming indexes or launching any worker that could serve/dispatch work.
    # Key-free demo: enabled() is false, so no engine or network connection.
    if control_plane.enabled():
        await run_in_threadpool(control_plane.runtime_role_status)
        # Tenancy policy: push LAURA_SHARED_DOMAIN_ORGS into the durable
        # resolver (ensure_user's domain-routing gate). Best-effort — the
        # migration-seeded default is personal-first, the safe direction.
        await run_in_threadpool(control_plane.sync_policy_flags)

    _prebuild_indexes()

    if settings.autopilot_nudge:
        async def _nudge_loop() -> None:
            while not _shutting_down:
                if autopilot.nudge_due():
                    await run_in_threadpool(autopilot.run_nudge)
                await asyncio.sleep(60)

        asyncio.create_task(_nudge_loop())

    if settings.gmail_watch_enabled:
        asyncio.create_task(_gmail_watch_loop())

    if settings.vendor_alerts_enabled:
        # Daily vendor subscription/credit watchdog: ElevenLabs characters,
        # Google refresh token, Recall/LLM keys, RunPod balance. Posts the
        # non-ok items to SLACK_WEBHOOK_URL; GET /health/vendors on demand.
        asyncio.create_task(_vendor_watch_loop())

    if settings.reconcile_enabled:
        # Backstop that finalizes sessions whose Recall bot is terminal but whose
        # status webhook never arrived — keeps the per-minute meter from leaking.
        asyncio.create_task(_reconcile_sessions_loop())

    async def _outbox_loop() -> None:
        while not _shutting_down:
            try:
                # SQLite demo reconciliation is harmless; production claims
                # durable Postgres rows with SKIP LOCKED + expiring leases.
                await run_in_threadpool(outbox.reconcile_sessions)
                await run_in_threadpool(outbox.process_due)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never log payload, URL, org or action text
                print(
                    f"[outbox] worker iteration failed: {type(exc).__name__}",
                    flush=True,
                )
            await asyncio.sleep(5)

    # Retain the task so shutdown cancels and awaits it deterministically.
    # Delivery ownership is the committed row, not this in-memory task.
    outbox_task = asyncio.create_task(_outbox_loop())

    if knowledge.enabled():
        # Company Brain ingest worker (M1): claims durable knowledge_sync_jobs
        # with SKIP LOCKED + leases — same ownership model as the outbox. On
        # boot it re-enqueues one index rebuild per org with published chunks,
        # so the in-memory per-org index files regenerate from Postgres after
        # a deploy wiped the disk. Never on the live transcript path.
        async def _knowledge_loop() -> None:
            from .knowledge import dal as knowledge_dal
            from .knowledge import ingest as knowledge_ingest

            try:
                await run_in_threadpool(knowledge_dal.enqueue_boot_rebuilds)
            except Exception as exc:  # noqa: BLE001 — boot rebuild is best-effort
                print(
                    f"[knowledge] boot rebuild enqueue failed: {type(exc).__name__}",
                    flush=True,
                )
            while not _shutting_down:
                try:
                    await run_in_threadpool(knowledge_ingest.process_due)
                    # Multi-instance convergence: instances don't share a
                    # disk, so EVERY instance periodically re-derives its own
                    # index files from the durable epoch (throttled inside).
                    await run_in_threadpool(
                        knowledge_ingest.refresh_local_indexes
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # never log content, filenames, or org text
                    print(
                        f"[knowledge] worker iteration failed: {type(exc).__name__}",
                        flush=True,
                    )
                await asyncio.sleep(5)

        asyncio.create_task(_knowledge_loop())

    if settings.elevenlabs_api_key:
        # Pre-synthesize the fixed conversational furniture so the FIRST
        # ack/backchannel/goodbye of a meeting comes from cache, not a
        # vendor round-trip. Fire-and-forget; never delays boot.
        asyncio.create_task(_prewarm_tts_cache())

    try:
        yield
    finally:
        _shutting_down = True
        outbox_task.cancel()
        try:
            await outbox_task
        except asyncio.CancelledError:
            pass
        print("[gmail-watch] shutdown signal — watcher draining", flush=True)


from .api import pages, granola, oauth, avatars_api, meetings, health, console  # extracted routes

app = FastAPI(title="Callable AI Process Avatar", lifespan=_lifespan)
# Production hardening: per-IP rate limiting on the public/expensive/unauth demo
# endpoints + security response headers. Registered BEFORE the routers so it
# wraps them; never touches the live-meeting path (ws/webhook/avatar/sessions).
# Avatar pages stay iframe-embeddable (no X-Frame-Options). See security.py.
security.install(app)
app.include_router(tts.router)  # POST /tts (open-source avatar voice)
app.include_router(org_api.router)  # /org/* — org-memory seam for surfaces (#48)
app.include_router(auth.router)  # /auth/* — dashboard login (Google Sign-In)
app.include_router(billing.router)  # /billing/* + signed /webhooks/stripe
app.include_router(dashboard.router)  # /dashboard — owner control view
from .knowledge import router as knowledge_router  # noqa: E402

app.include_router(knowledge_router.router)  # /org/knowledge + dashboard twin (M1)
app.include_router(pages.router)  # static pages + avatar assets (api/pages.py)
app.include_router(granola.router)  # /granola/* (api/granola.py)
app.include_router(oauth.router)  # /oauth/* (api/oauth.py)
app.include_router(avatars_api.router)  # /avatars* (api/avatars_api.py)
app.include_router(meetings.router)  # /ledger, /meetings* (api/meetings.py)
app.include_router(health.router)  # /health, /recall/status, /gmail/status ... (api/health.py)
app.include_router(console.router)  # /, /demo/*, /live/* (api/console.py)

# Meeting-bound GPU runtime re-checks the live session count before it stops
# the photoreal box (a new meeting may have started during the grace window).
gpu_runtime.configure(lambda: len(store.all_sessions()))
runpod_runtime.configure(lambda: len(store.all_sessions()))

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
REPO_ROOT_DIR = Path(__file__).resolve().parents[2]
from app.api.deps import EMAIL_RE, _split_emails, _calendar_target_emails, _line_for, _gmail_state  # noqa: E402
from .meeting.lifecycle import (  # noqa: E402  (hoisted lifecycle core; re-import = compat)
    _start_avatar_session, _finalize_session, _finalize_session_locked,
    _close_usage_for, _assign_usage_bot_id, _leave_confirmed_stopped, _retry_leave,
    _bot_reports_terminal, _recall_list_headers, _bot_status_code, _bot_meeting_key,
    _bot_variant_rank, _avatar_asana_enabled, _merge_action_items, _stamp_action_routing,
    _finalizing, _BOT_TERMINAL, _BOT_VARIANT_RANK, _LEAVE_GONE_STATUSES,
)
ATTENDEE_CONTAINER_KEYS = {
    "attendee",
    "attendees",
    "participant",
    "participants",
    "guest",
    "guests",
    "invitee",
    "invitees",
    "recipient",
    "recipients",
}






def _prebuild_indexes() -> None:
    """Build each avatar's RAG index on boot so the demo works with no setup.

    Free & instant with the default hash embedder; skipped if already current.
    """
    # Resolve the local embedder's availability FIRST (fail-soft: a HuggingFace
    # outage degrades to hash instead of hanging boot until the App Runner
    # health check kills the deploy — root cause of the 2026-07-16 rollback).
    # Must precede any ensure_index: the index signature reads the EFFECTIVE
    # provider, which is only known after this attempt.
    from . import embeddings

    embeddings.warmup()
    for aid in avatars.list_ids():
        try:
            avatar = avatars.load(aid)
            ensure_index(avatar)
            # Self-knowledge pack (about/): separate index, only consulted for
            # self-questions. No-op for avatars without an about/ folder.
            ensure_about_index(avatar)
            # Load the index into cache + warm the embedder now, so the first
            # LIVE question doesn't pay the cold-start (1-3s) on the meeting path.
            warm_index(avatar)
        except Exception as e:  # a bad avatar shouldn't stop the server
            print(f"[startup] could not index avatar '{aid}': {e}")


# ─────────────── Gmail watcher: "Add people" → auto-join ────────────────
# Poll Laura's inbox for Google Meet invitation emails (sent by Meet's native
# "Add people") and send the bot into that meeting. See gmail_watcher.py.
_gmail_seen_ids: set[str] = set()
# Set when the instance is being drained (deploy/rollout). The Gmail watcher stops
# dispatching bots the moment this flips, so a draining OLD instance never races the
# NEW instance to put a second bot in the same meeting during a deploy overlap.
_shutting_down = False

# Spoken the instant she decides to web-search, so the ~4s search isn't dead air.

# Spoken when a NON-search answer is taking a beat (tool round-trips, slow
# provider) — an acknowledgment beats dead air on the interactive avatar.

# How long a /live/act answer may take before she speaks an acknowledgment
# filler. Below this, a filler is just noise in front of an instant answer.

# bot_ids whose finalize is currently in flight. Recall emits bot.call_ended
# THEN bot.done (both terminal) as SEPARATE concurrent webhook POSTs, and the
# reconciliation loop can race them — this in-flight set makes _finalize_session
# safe to call from all three sources without double-delivering session.ended.
_LIVE_REPAIR_RE = re.compile(
    r"\b(can you hear|do you hear|hear me|are you there|hello|hi laura|"
    r"doesn'?t work|not working|is broken|no response|answer me|"
    r"what can you do|help me)\b",
    re.IGNORECASE,
)








def _should_repair_silent_answer(called: bool, text: str) -> bool:
    return called or bool(_LIVE_REPAIR_RE.search(text or ""))


def _silent_answer_repair_line(avatar: avatars.Avatar) -> str:
    return (
        f"I can hear you, but I didn't catch a clear question. "
        f"Ask me about our processes or the portfolio — for example: "
        f"{avatar.name}, what are we missing before go-live?"
    )


def _reconcile_duplicate_bots(meeting_url: str, my_bot_id: str) -> None:
    """If a race (deploy overlap: two instances polling) put more than one bot in
    the meeting, keep the best bot and make the rest leave.

    Best means the higher-powered output-media variant (web_gpu/web_4_core) over
    the default 250m web bot; ties are broken by earliest-created. Both instances
    compute the same ranking from the same Recall data, so the duplicate resolves
    deterministically and the survivor is the smooth bot, not the laggy default.
    """
    if not settings.recall_api_key.strip():
        return  # no key → can't query Recall (offline/demo); nothing to reconcile
    key = ledger.meeting_key(meeting_url)
    if not key:
        return
    try:
        r = httpx.get(
            f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/",
            headers=_recall_list_headers(),
            timeout=20.0,
        )
        r.raise_for_status()
        active = []
        for bot in (r.json().get("results") or [])[:25]:
            if _bot_meeting_key(bot) != key:
                continue
            status = (bot.get("status_changes") or [{}])[-1].get("code")
            if status not in _BOT_TERMINAL:
                active.append(
                    (
                        _bot_variant_rank(bot),
                        bot.get("created_at") or "",
                        bot.get("id"),
                    )
                )
        if len(active) <= 1:
            return
        active.sort()  # best variant first, then earliest; keep [0], evict the rest
        for _rank, _created, bid in active[1:]:
            if not bid:
                continue
            try:
                recall_client.leave_call(bid)
            except Exception:
                pass
    except Exception:
        pass


def _meeting_has_active_bot(meeting_url: str) -> bool:
    """True if Recall already has a non-terminal bot in this meeting.

    Durable, cross-instance dedup: the in-memory / per-process guards can miss a
    duplicate across a deploy overlap or a second invite email, so we check
    Recall itself (the source of truth) before dispatching another bot. Matching
    is platform-aware via ledger.meeting_key (Meet/Zoom/Teams) — the old
    Meet-only regex never matched a Zoom/Teams link, so this guard no-op'd there.
    """
    if not settings.recall_api_key.strip():
        return False  # no key → can't query Recall (offline/demo); pre-guard behavior
    key = ledger.meeting_key(meeting_url)
    if not key:
        return False
    try:
        r = httpx.get(
            f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/",
            headers=_recall_list_headers(),
            timeout=20.0,
        )
        r.raise_for_status()
        for bot in (r.json().get("results") or [])[:25]:
            if _bot_meeting_key(bot) != key:
                continue
            status = (bot.get("status_changes") or [{}])[-1].get("code")
            if status not in _BOT_TERMINAL:
                return True
    except Exception:
        pass
    return False


# Per-meeting serialization for the manual-start guard→create window. Two
# concurrent POST /sessions/start for the SAME meeting must not both pass the
# dedup checks and both create a bot (two bots → two per-minute meters in one
# call). Keyed by ledger.meeting_key so Meet/Zoom/Teams links serialize per
# meeting; DIFFERENT meetings hold different locks and still dispatch in
# parallel. A WeakValueDictionary drops a lock once no request references it (no
# unbounded growth), while any concurrent waiter keeps its own strong ref so the
# same key always resolves to the same lock object.
_start_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _start_lock_for(meeting_url: str) -> asyncio.Lock:
    key = ledger.meeting_key(meeting_url)
    lock = _start_locks.get(key)
    if lock is None:  # create-on-demand; get→create is synchronous (atomic here)
        lock = asyncio.Lock()
        _start_locks[key] = lock
    return lock


# Seeding cutoff for the gmail watcher's FIRST poll after boot: mail older
# than this is history (never join it), mail fresher is a LIVE invite that
# just happened to land during an instance flip. Before this cutoff existed,
# the first pass swallowed EVERYTHING — with several deploys back-to-back
# (2026-07-10: five in ~40min, each restart ≈ a fresh first pass) a real
# invite sent mid-deploy was silently marked seen and the bot never joined.
_GMAIL_SEED_FRESH_SECONDS = 600.0


async def _vendor_watch_loop() -> None:
    """Daily vendor watchdog (see vendor_health.py): run every check, post the
    non-ok ones to Slack. First run shortly after boot so a dead key or an
    expired Google token is flagged within minutes of a deploy, not tomorrow."""
    await asyncio.sleep(120)  # let the instance settle first
    while True:
        if _shutting_down:
            return
        try:
            results = await run_in_threadpool(vendor_health.run_checks)
            text = vendor_health.slack_text(results)
            if text and settings.slack_webhook_url:
                await run_in_threadpool(actions.post_to_slack, text)
            bad = [r for r in results if r["status"] in ("warn", "crit")]
            print(
                f"[vendors] check: {len(results) - len(bad)} ok, {len(bad)} "
                f"da attenzionare{' (postato su Slack)' if text and settings.slack_webhook_url else ''}",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001 — the watchdog never dies
            print(f"[vendors] check fallito: {e}", flush=True)
        await asyncio.sleep(max(1.0, settings.vendor_check_hours) * 3600)


async def _gmail_watch_loop() -> None:
    seeded = False  # first pass records existing mail; only FRESH invites join
    while True:
        if _shutting_down:
            print("[gmail-watch] instance draining — watcher stopped", flush=True)
            return
        await asyncio.sleep(settings.gmail_poll_seconds)
        if not settings.gmail_watch_enabled or _shutting_down:
            continue
        try:
            rt = await run_in_threadpool(gmail_watcher.refresh_token)
            if not rt:
                _gmail_state["last_error"] = (
                    "no refresh token — run /oauth/google/connect (with gmail scope)"
                )
                continue
            token = await run_in_threadpool(gmail_watcher.access_token, rt)
            new = await run_in_threadpool(
                gmail_watcher.poll_new_invites, token, _gmail_seen_ids
            )
            _gmail_state["last_poll"] = time.time()
            _gmail_state["last_error"] = ""
            if not seeded:
                seeded = True
                # Keep only invites received in the last few minutes: a live
                # "Add people" that landed during the restart still joins,
                # genuinely old mail stays history. Duplicate joins across the
                # old/new instance are already prevented downstream by the
                # durable guards (store.is_scheduled + _meeting_has_active_bot
                # + _reconcile_duplicate_bots).
                cutoff = time.time() - _GMAIL_SEED_FRESH_SECONDS
                fresh = [n for n in new if n[3] and n[3] >= cutoff]
                if fresh:
                    print(
                        f"[gmail-watch] boot seeding: {len(new) - len(fresh)} old "
                        f"message(s) recorded, {len(fresh)} fresh invite(s) kept",
                        flush=True,
                    )
                new = fresh
            for _mid, url, invite_addrs, _received_at in new:
                if _shutting_down:
                    break  # draining — don't start new bots
                if store.is_scheduled(url):
                    continue
                # Durable cross-instance guard against duplicate bots.
                if await run_in_threadpool(_meeting_has_active_bot, url):
                    store.mark_scheduled(url)
                    continue
                try:
                    # A plus-tagged recipient (laura.ai.122222+cedric@…) is
                    # that avatar's email — route the invite to it.
                    aid = (
                        avatars.from_invite_email(
                            invite_addrs, _calendar_target_emails()
                        )
                        or settings.default_avatar_id
                    )
                    # Org attribution: the gmail path lands on the DEMO org (no
                    # org_id passed). The sender headers (Reply-To/From) are
                    # unauthenticated and this watcher reads one shared inbox,
                    # so trusting them to pick a tenant would let a forged
                    # header bill/arm someone else's org — see
                    # _org_for_calendar_event. Personal attribution here waits
                    # on a per-user mailbox (like #247 did for the calendar).
                    res = await _start_avatar_session(url, aid)
                    store.mark_scheduled(url)
                    _gmail_state["joined"].append(
                        {"meeting_url": url, "bot_id": res["bot_id"], "at": time.time()}
                    )
                    print(f"[gmail-watch] joined {url} via bot {res['bot_id']} as {aid}", flush=True)
                    # Resolve any deploy-overlap duplicate: let a racing bot register,
                    # then keep the best Recall variant and drop the rest.
                    await asyncio.sleep(4)
                    await run_in_threadpool(_reconcile_duplicate_bots, url, res["bot_id"])
                except Exception as e:
                    print(f"[gmail-watch] failed to join {url}: {e}", flush=True)
        except Exception as e:
            _gmail_state["last_error"] = str(e)


# bot_id -> consecutive GET /bot/{id} 404 count. A lone 404 can be a transient
# Recall blip or a just-created scheduled bot; only after several in a row do we
# treat the bot as gone. Pruned every pass for bots that left the store by any
# path, so it can't accumulate dead entries.
_reconcile_missing: dict[str, int] = {}
_RECONCILE_MISSING_LIMIT = 3




# Recall status codes that mean the bot is IN the meeting and the per-minute
# meter is running — the usage clock (entitlements.mark_in_call) starts on the
# FIRST of these. Status-based on purpose: a silent meeting consumes minutes
# exactly like a talkative one (Recall bills either way).
_IN_CALL_CODES = {"in_call_recording", "in_call_not_recording"}


def _status_change_epoch(
    bot: dict, codes: set[str], *, first: bool = True
) -> float | None:
    """Epoch of the first (or last) status_changes entry whose code is in
    ``codes``. Recall stamps every status with its own created_at — the
    authoritative record of when the meter actually started/stopped, immune
    to our own polling lag. None when absent or unparsable."""
    changes = bot.get("status_changes") or []
    for ch in changes if first else reversed(changes):
        if ch.get("code") in codes:
            ts = str(ch.get("created_at") or "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                return None
            # A timestamp with no offset would otherwise be read as LOCAL time —
            # off by the host's UTC offset (hours of phantom consumed_seconds).
            # Recall stamps UTC; treat a naive value as UTC.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    return None






async def _abandon_orphan_session(bot_id: str) -> None:
    """Drop a local session whose Recall bot no longer exists and that never
    captured anything — a cancelled or no-show scheduled bot. Stops the Anam
    conversation if one was opened (best-effort; the Recall bot is already gone,
    so no Recall meter remains), then removes the session. NO artifact and NO
    session.ended: the meeting never happened, so the orchestrator must not hear
    it 'ended' (that would be a phantom completed-meeting signal).

    Runs under the same ``_finalizing`` guard as ``_finalize_session`` so it
    can't race a concurrent finalize of the same bot: if a real finalize already
    holds the guard it owns this bot and we no-op; otherwise we hold it across the
    Anam teardown + remove so no finalize slips past ``store.remove``."""
    session = store.get(bot_id)
    if session is None or bot_id in _finalizing:
        return
    _finalizing.add(bot_id)
    try:
        if session.anam_conversation_id:
            try:
                await run_in_threadpool(
                    anam_client.end_conversation, session.anam_conversation_id
                )
            except Exception:
                pass
        # PR B: a no-show bot consumed nothing — release its usage row so the
        # org's one-active-meeting slot frees up (idempotent; never raises).
        await _close_usage_for(session.org_id, bot_id, None, "never_joined")
        store.remove(bot_id)
    finally:
        _finalizing.discard(bot_id)


# Recall statuses that CONFIRM a bot is no longer billing: the bot is genuinely
# gone. Everything else — 401/403 (rotated/expired key), 429 (rate limit), any
# 5xx, network/other — leaves the meter-stop UNVERIFIED: the bot may still be
# live and billing, so the session must be kept and the leave retried.








async def _reconcile_once() -> None:
    """One reconciliation pass: finalize every active session whose Recall bot is
    terminal, and drop orphaned sessions whose bot Recall no longer knows about.
    Extracted from the loop so it is unit-testable without touching asyncio.sleep.
    Best-effort per session — a bad poll skips that bot, never the whole pass.

    PR B threads the usage clock + entitlement enforcement through this SAME
    pass (gate at start, clock at reconcile — nothing on the live hot path):
    - a store session whose usage row has no in_call_at yet gets its clock
      started from Recall's OWN in-call status timestamp (mark_in_call);
    - live sessions get the spoken ~5min/~1min warnings and the hard stop
      at/after the deadline (the hardened finalize path: verified leave,
      artifact preserved, usage closed 'limit_reached');
    - durable usage rows whose bot is NOT in the (ephemeral) local store —
      a redeploy wiped it — are STILL enforced straight from Recall
      (_reconcile_usage_orphan): the restart-restore that heals the
      orphan-meter class. All of it disabled when the control plane is
      (the key-free demo runs this pass byte-identically to before)."""
    active = store.all_sessions()  # a COPY — safe while finalize removes
    # Durable pending/active usage rows, fetched ONCE per pass (threadpooled —
    # sync engine). {} when the control plane is disabled (no engine touched)
    # or momentarily unreachable (skip usage work this pass, never the pass).
    usage_by_bot: dict[str, dict] = {}
    usage_fetch_ok = False  # only self-heal / orphan-sweep on a GOOD read
    if control_plane.enabled():
        try:
            rows = await run_in_threadpool(entitlements.open_usage_rows)
            usage_by_bot = {u["bot_id"]: u for u in rows}
            usage_fetch_ok = True
        except Exception:  # noqa: BLE001 — a PG blip must not kill the meter backstop
            usage_by_bot = {}
            usage_fetch_ok = False
    # Prune miss-counters for bots that already left the store by ANY path
    # (finalized via webhook, cancelled) so the dict can't grow dead entries.
    # Usage-tracked orphans (restart restore below) keep their counters.
    live_ids = {s.bot_id for s in active} | set(usage_by_bot)
    for gone in [b for b in _reconcile_missing if b not in live_ids]:
        _reconcile_missing.pop(gone, None)
    for session in active:
        if _shutting_down:
            break
        bid = session.bot_id
        # A prior finalize built + delivered this session but its Recall
        # meter-stop failed (5xx / network) and kept it for retry. The bot may
        # still be live, so the terminal-status path below would never fire —
        # retry the leave directly. On success the session is dropped; the
        # artifact already went out, so there is NO re-delivery.
        if getattr(session, "leave_pending", False):
            # P0: finalize may have kept the session after BOTH the provisional
            # usage-id swap and the first Recall leave failed. Repair the meter
            # before retrying leave; otherwise this early continue can strand a
            # live real bot behind a pending:<uuid> row forever.
            usage = usage_by_bot.get(bid)
            if (
                usage is None
                and usage_fetch_ok
                and session.org_id
                and not bid.startswith("pending:")
            ):
                usage = await _repair_untracked_usage(
                    session, usage_by_bot, force_finalize=False
                )
            if usage is not None and usage.get("in_call_at") is None:
                try:
                    status = await run_in_threadpool(
                        lambda b=bid: httpx.get(
                            f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/{b}/",
                            headers=_recall_list_headers(),
                            timeout=20.0,
                        )
                    )
                    status.raise_for_status()
                    bot = status.json()
                    code = _bot_status_code(bot)
                    started = _status_change_epoch(bot, _IN_CALL_CODES)
                    if started is not None or code in _IN_CALL_CODES:
                        started_at = started or time.time()
                        deadline = await run_in_threadpool(
                            entitlements.mark_in_call,
                            session.org_id,
                            bid,
                            started_at,
                        )
                        usage["in_call_at"] = started_at
                        usage["deadline"] = deadline
                except Exception:
                    # Leave is still retried below. A status/DB blip gets
                    # another repair+clock attempt on the next pass.
                    pass
            await _retry_leave(bid, session)
            continue
        try:
            r = await run_in_threadpool(
                lambda b=bid: httpx.get(
                    f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/{b}/",
                    headers=_recall_list_headers(),
                    timeout=20.0,
                )
            )
            if r.status_code == 404:
                misses = _reconcile_missing.get(bid, 0) + 1
                _reconcile_missing[bid] = misses
                if misses >= _RECONCILE_MISSING_LIMIT:
                    _reconcile_missing.pop(bid, None)
                    # Bot is gone from Recall. If it captured a transcript it was a
                    # real meeting whose bot got cleaned up -> finalize + deliver.
                    # If it never produced a line it's a cancelled/no-show
                    # scheduled bot -> drop the orphan WITHOUT a phantom
                    # session.ended for a meeting that never happened.
                    if session.transcript:
                        await _finalize_session(bid, source="reconcile")
                    else:
                        await _abandon_orphan_session(bid)
                continue
            r.raise_for_status()
            _reconcile_missing.pop(bid, None)  # reachable again
            bot = r.json()
            code = _bot_status_code(bot)
            usage = usage_by_bot.get(bid)
            # BLOCKER 1 self-heal: a LIVE local session with an org but NO usage
            # row means the provisional-id swap failed after create_bot — the
            # bot is running untracked. Re-associate the stranded provisional
            # row (or open a fresh one); if it genuinely can't be metered, cut
            # it off rather than let it bill free. Only on a good usage read and
            # a real (non-provisional) bot id.
            if (
                usage is None
                and usage_fetch_ok
                and session.org_id
                and not bid.startswith("pending:")
            ):
                usage = await _repair_untracked_usage(session, usage_by_bot)
                if store.get(bid) is None:
                    continue  # repair force-finalized it (can't meter) — gone
            if usage is not None and usage.get("in_call_at") is None:
                # Start the usage clock on Recall's OWN in-call timestamp —
                # status-based, so a silent meeting consumes exactly like a
                # talkative one. Idempotent; a PG blip just retries next pass.
                started = _status_change_epoch(bot, _IN_CALL_CODES)
                if started is not None or code in _IN_CALL_CODES:
                    try:
                        deadline = await run_in_threadpool(
                            entitlements.mark_in_call, session.org_id, bid, started or time.time()
                        )
                        usage["in_call_at"] = started or time.time()
                        usage["deadline"] = deadline
                    except Exception:  # noqa: BLE001 — clock starts next pass
                        pass
            if code in _BOT_TERMINAL:
                # notify_failed fires ONCE, inside the guarded finalize body, so a
                # fatal seen by both this poll and the webhook notifies Cedric once.
                # The terminal status carries Recall's own end timestamp — the
                # authoritative consumed_seconds source for the usage close.
                await _finalize_session(
                    bid,
                    source="reconcile",
                    failed_code=code,
                    usage_end_epoch=_status_change_epoch(bot, {code}, first=False),
                    bot_terminal=True,  # Recall reports terminal → meter already off
                )
                continue
            # Live bot with a usage deadline: warn near it, hard-stop at it.
            deadline = (usage or {}).get("deadline")
            if deadline:
                if time.time() >= deadline:
                    # Same hardened finalize as every other end: verified
                    # leave_call (#148), artifact built + preserved, usage
                    # closed 'limit_reached' (first close wins).
                    await _finalize_session(
                        bid, source="reconcile", usage_reason="limit_reached"
                    )
                else:
                    await _usage_warn(session, deadline)
        except Exception:
            continue  # best-effort: a bad poll must never break the loop
    # ── restart restore (PR B): enforce usage rows the local store forgot ──
    if usage_fetch_ok and usage_by_bot:
        local_ids = {s.bot_id for s in active}
        # Orgs that still have a LIVE local session: a stranded provisional row
        # of such an org belongs to its live meeting (one row per org) — the
        # orphan sweep must NOT free that slot (BLOCKER 1). list() so a repair
        # that mutated usage_by_bot mid-pass can't trip "changed during iteration".
        live_orgs = {s.org_id for s in active}
        for bid, usage in list(usage_by_bot.items()):
            if _shutting_down:
                break
            if bid in local_ids:
                continue
            await _reconcile_usage_orphan(bid, usage, live_orgs)


async def _repair_untracked_usage(
    session: store.Session,
    usage_by_bot: dict[str, dict],
    *,
    force_finalize: bool = True,
) -> dict | None:
    """BLOCKER 1 self-heal: a LIVE local session whose usage row is missing —
    the provisional-id swap failed after create_bot, so the real bot runs
    untracked (no clock, no deadline, no cutoff → unmetered forever). Re-point
    the org's stranded provisional row at this real bot id, else open a fresh
    row; if the org genuinely can't host it (budget exhausted, or the slot is
    held by a DIFFERENT live meeting), FORCE-FINALIZE so the meter stops the
    hardened way instead of billing free. Returns the usage dict to meter from
    now on, or None (billing down this pass — bot kept; or force-finalized —
    the caller checks store.get()). At most one pending/active row exists per
    org (the partial unique index), so the org's row, if any, is unambiguous."""
    org = session.org_id
    real = session.bot_id
    org_row = next(
        (u for u in usage_by_bot.values() if u.get("org_id") == org), None
    )
    try:
        if (
            org_row is not None
            and str(org_row["bot_id"]).startswith("pending:")
            and org_row.get("in_call_at") is None
        ):
            prov = org_row["bot_id"]
            if await run_in_threadpool(entitlements.assign_bot_id, org, prov, real):
                usage_by_bot.pop(prov, None)
                org_row["bot_id"] = real
                usage_by_bot[real] = org_row
                print(
                    f"[usage] re-associated stranded usage row to bot={real}",
                    flush=True,
                )
                return org_row
            # reassign found no row (raced closed) → the slot is free; fall
            # through to open a fresh one.
        gate = await run_in_threadpool(
            entitlements.open_usage, org, real, session.avatar_id
        )
        if gate is not None and gate.get("ok"):
            row = {
                "org_id": org, "bot_id": real, "avatar_id": session.avatar_id,
                "state": "pending", "created_at": time.time(),
                "in_call_at": None, "deadline": None,
            }
            usage_by_bot[real] = row
            print(
                f"[usage] opened repair usage row for untracked bot={real}",
                flush=True,
            )
            return row
    except entitlements.EntitlementsUnavailable:
        return None  # billing down this pass — retry next pass, bot kept
    # Can't meter it (org exhausted, slot held by another live meeting). A
    # normal live session is force-finalized. A leave-pending session was
    # ALREADY finalized/delivered, so its caller retries the meter-stop without
    # rebuilding or re-delivering the artifact.
    if force_finalize:
        print(f"[usage] cannot meter untracked bot={real} — cutting off", flush=True)
        await _finalize_session(real, source="reconcile", usage_reason="limit_reached")
    return None


async def _reconcile_usage_orphan(
    bid: str, usage: dict, live_orgs: set[str] | None = None
) -> None:
    """Restart restore: a durable pending/active usage row whose bot is NOT in
    the local store — the process restarted (App Runner's store is ephemeral)
    or a provisional gate row was stranded mid-dispatch. Without this, a
    redeploy would both leak the Recall meter AND grant unmetered minutes; the
    deadline is enforced straight from Recall instead. Best-effort per row —
    never raises, never breaks the pass."""
    try:
        if bid.startswith("pending:"):
            # The gate's provisional id: create_bot never completed, or the
            # id swap was interrupted. If the OWNING org still has a live local
            # session, this provisional belongs to THAT live meeting (one row
            # per org) and the store-loop self-heal will re-associate it — do
            # NOT free the slot out from under a billing bot (BLOCKER 1). Only
            # when the org has no live session, and after a grace window, is it
            # a true orphan → close it (consumes 0: it never went in-call).
            if live_orgs is not None and usage.get("org_id") in live_orgs:
                return
            if time.time() - float(usage.get("created_at") or 0) > 600:
                await run_in_threadpool(entitlements.close_usage, usage["org_id"], bid, 0, "orphaned")
            return
        if not settings.recall_api_key.strip():
            return  # no key → can't reach Recall; leave the row for a keyed instance
        r = await run_in_threadpool(
            lambda b=bid: httpx.get(
                f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/{b}/",
                headers=_recall_list_headers(),
                timeout=20.0,
            )
        )
        if r.status_code == 404:
            # Same consecutive-miss patience as the store loop: one 404 can be
            # a Recall blip; three in a row means the bot is genuinely gone.
            misses = _reconcile_missing.get(bid, 0) + 1
            _reconcile_missing[bid] = misses
            if misses >= _RECONCILE_MISSING_LIMIT:
                _reconcile_missing.pop(bid, None)
                await _close_usage_for(usage["org_id"], bid, None, "bot_missing")
            return
        r.raise_for_status()
        _reconcile_missing.pop(bid, None)
        bot = r.json()
        code = _bot_status_code(bot)
        if code in _BOT_TERMINAL:
            # Ended while we were down: consumed from Recall's own timestamps.
            await _close_usage_for(
                usage["org_id"], bid,
                _status_change_epoch(bot, {code}, first=False), "ended"
            )
            return
        # Still live: restore the clock if the row never got one…
        deadline = usage.get("deadline")
        if usage.get("in_call_at") is None:
            started = _status_change_epoch(bot, _IN_CALL_CODES)
            if started is not None or code in _IN_CALL_CODES:
                deadline = await run_in_threadpool(
                    entitlements.mark_in_call, usage["org_id"], bid, started or time.time()
                )
        # …then enforce the deadline. No local session → no artifact to build;
        # just stop the meter the VERIFIED way (same classification as #148)
        # and close the row. An unverified leave keeps the row for next pass.
        if deadline and time.time() >= deadline:
            try:
                await run_in_threadpool(recall_client.leave_call, bid)
            except Exception as e:  # noqa: BLE001 — classified below
                if not _leave_confirmed_stopped(e):
                    return  # UNVERIFIED — bot may still bill; retry next pass
            await _close_usage_for(usage["org_id"], bid, None, "limit_reached")
    except Exception:  # noqa: BLE001 — an orphan hiccup must never break the pass
        pass


async def _reconcile_sessions_loop() -> None:
    """Backstop for auto-finalize: poll Recall for each active session's bot and
    finalize any that Recall reports terminal but we still hold live locally.

    Terminal status events reach Laura only via the account status-change webhook
    (Recall structurally can't put them on the per-bot realtime endpoint). If that
    webhook is un/mis-configured or a delivery is dropped, the session sticks in
    'in_progress' forever and the per-minute Anam meter leaks — and a bot.fatal
    can fail to reach the webhook at all, so this poll is the ONLY guaranteed
    recovery for it.
    """
    while True:
        if _shutting_down:
            return
        await asyncio.sleep(settings.reconcile_poll_seconds)
        if not settings.reconcile_enabled or _shutting_down:
            continue
        await _reconcile_once()




# ───────────────────────────── health ──────────────────────────────


# ─────────────────────────── demo console ──────────────────────────
# Everything below runs WITHOUT the live-meeting vendors (Recall/Anam/
# ElevenLabs). It exercises the brain + RAG directly so you can prove the value
# with zero keys (BRAIN_PROVIDER=stub) or one key (BRAIN_PROVIDER=anthropic).
















# ── live avatar preview (Anam face + Claude brain, NO meeting vendor) ──
# Lets you SEE the talking avatar answer from the docs without Recall/ngrok.


# TTS for the open-source avatar page lives in its own router (tts.py) — a
# self-contained concern with no coupling to the live meeting path.










# ── Recall diagnostics: non-secret readiness/auth check for live meetings ──


# ──────────────────────── session lifecycle ────────────────────────
class StartRequest(BaseModel):
    meeting_url: str
    avatar_id: str = ""  # empty -> settings.default_avatar_id
    join_at: Optional[str] = None  # ISO 8601; set (>=10 min out) to schedule the bot
    # CEDRIC: orchestrator integration fields — all optional; models, the auth
    # gate, and validation live in the `cedric` package (docs/04-api-contract.md).
    context: Optional[cedric.MeetingContext] = None
    callback_url: Optional[str] = None   # where session.status/.ended events go
    context_url: Optional[str] = None    # re-fetched at join time for a fresh brief
    external_ref: Optional[dict] = None  # opaque, echoed verbatim in callbacks




def _existing_session_clash(meeting_url: str, caller_org: str) -> JSONResponse | None:
    """The 409 to return when a local-store session is already booked for this
    exact meeting_url — one live/scheduled booking per URL (rebooking must cancel
    first, else two bots + two per-minute meters land in one call). None when
    there is no clash. Another tenant's clashing bot_id is never leaked."""
    for existing in store.all_sessions():
        if existing.meeting_url == meeting_url:
            if existing.org_id != caller_org:
                return JSONResponse(
                    {"error": "a session already exists for this meeting_url"},
                    status_code=409,
                )
            return JSONResponse(
                {
                    "error": "a session already exists for this meeting_url",
                    "bot_id": existing.bot_id,
                },
                status_code=409,
            )
    return None


async def _reconcile_after_start(meeting_url: str, bot_id: str) -> None:
    """Give a racing duplicate bot a moment to register with Recall, then keep
    the best variant and drop the rest — same as the Gmail auto-join loop, but
    fire-and-forget so the /sessions/start response returns immediately."""
    try:
        await asyncio.sleep(4)
        await run_in_threadpool(_reconcile_duplicate_bots, meeting_url, bot_id)
    except Exception:
        pass


def _schedule_start_reconcile(meeting_url: str, bot_id: str) -> None:
    """Schedule the post-start duplicate-bot reconcile without blocking the
    response (the Gmail loop can await it inline; a request handler cannot)."""
    try:
        asyncio.create_task(_reconcile_after_start(meeting_url, bot_id))
    except RuntimeError:
        pass  # no running loop to schedule on (shouldn't happen in the handler)


def _org_token_bearer_org(request: Request) -> Optional[str]:
    """The org owning the request's Bearer, when it is a PER-ORG machine token
    (org_tokens: durable control plane first, SQLite fallback). None for no/
    non-org bearers — including the GLOBAL laura_api_token, which keeps its
    demo-org behavior. Sibling of cedric.resolve_machine_org (PR D), which
    additionally maps the global bearer to the Demo org — kept separate so PR
    A's start/end/redeliver semantics stay untouched.
    A raw secret is compared/hashed, never logged. Called
    on /sessions/start, /sessions/{id}/end and /sessions/{id}/redeliver —
    control-plane paths, never the live hot path. SYNC (SQLite + optionally
    the Postgres control plane): async handlers must call it via
    run_in_threadpool so it never blocks the shared event loop (single
    instance — a blocked loop stalls every live meeting)."""
    provided = request.headers.get("authorization", "")
    if not provided.startswith("Bearer "):
        return None
    raw = provided[len("Bearer "):].strip()
    if not raw:
        return None
    global_token = settings.laura_api_token.strip()
    if global_token and hmac.compare_digest(raw, global_token):
        return settings.demo_org_id
    # Durable revocation is authoritative in production: never resurrect a
    # token from the ephemeral SQLite cache after Postgres rejects it.
    if control_plane.enabled():
        return control_plane.resolve_org_token(raw)
    return store.resolve_org_token(raw)


@app.post("/sessions/start")
async def start_session(req: StartRequest, request: Request) -> JSONResponse:
    # A logged-in human (dashboard cookie) or a machine bearer (Cedric). The
    # shared auth.gate closes the "login enabled + no token" hole: an anonymous
    # caller can NOT dispatch a per-minute bot on a login-protected deployment.
    # A valid cookie stamps the session with the user's org for later scoping.
    # A PER-ORG machine bearer (org_tokens) both authenticates the start and
    # scopes it to ITS org — the service twin of the cookie principal.
    user = auth.current_user(request)
    token_org: Optional[str] = None
    if user is None:
        # Threadpooled: the resolver is sync DB I/O (see its docstring).
        token_org = await run_in_threadpool(_org_token_bearer_org, request)
        if token_org is None:
            if err := auth.gate(request):
                return err
    # Browser/cookie users choose only meeting + avatar. Integration wiring
    # is server-owned per org; reject it before any vendor readiness check,
    # database enumeration or paid bot creation.
    if user is not None and (
        req.callback_url or req.context_url or req.external_ref
    ):
        return JSONResponse(
            {"error": "integration wiring is managed by your workspace"},
            status_code=400,
        )
    try:
        recall_client.assert_ready()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    brief = req.context.brief_markdown if req.context else ""  # CEDRIC
    if err := cedric.brief_too_large(brief):  # CEDRIC
        return err
    # The owning tenant: the logged-in user's org, else the org-token's org,
    # else the Demo org for the global-bearer/anon service path (Cedric
    # bearer, key-free demo). NEVER derived from a request body field or a
    # meeting participant (MULTI-TENANCY §0/§3.1) — StartRequest deliberately
    # has no org field; keep it that way.
    caller_org = user["org_id"] if user else (token_org or settings.demo_org_id)
    # Internal personas (INTERNAL_AVATAR_IDS) are not dispatchable by ANY
    # caller — same 404 an unknown avatar id gets (defense-in-depth while the
    # folder still exists; see config.internal_avatar_ids).
    if avatars.is_internal(req.avatar_id or settings.default_avatar_id):
        return JSONResponse({"error": "unknown avatar_id"}, status_code=404)
    # One live/scheduled booking per meeting URL: rebooking must cancel first
    # (otherwise two bots — and two per-minute meters — end up in one call).
    # Fast path: an obvious local-store clash needs no lock or Recall round-trip
    # (the common re-click of the same link).
    if clash := _existing_session_clash(req.meeting_url, caller_org):
        return clash

    if not cedric.request_integration_urls_allowed(req, caller_org):
        return JSONResponse(
            {"error": "callback_url/context_url must use the configured Cedric HTTPS origin"},
            status_code=400,
        )
    integration = cedric.build_integration(req, brief)  # CEDRIC
    if integration is not None:
        integration = {**integration, "org_id": caller_org}
    # Serialize the guard→create window PER MEETING so two concurrent starts for
    # the same link can't both pass the dedup checks and both create a bot.
    # DIFFERENT meetings hold different locks and still dispatch in parallel.
    async with _start_lock_for(req.meeting_url):
        # Re-check under the lock: a racing double-click may have created the
        # session between the fast-path check above and acquiring the lock.
        if clash := _existing_session_clash(req.meeting_url, caller_org):
            return clash
        # Durable cross-instance guard (Recall = source of truth): a redeploy can
        # wipe the local store while Recall still holds the live bot, so a
        # re-click would dispatch a SECOND bot + meter into the same call. The
        # Gmail/calendar paths already gate on this; wire it here too (now
        # platform-aware for Zoom/Teams as well as Meet).
        if await run_in_threadpool(_meeting_has_active_bot, req.meeting_url):
            return JSONResponse(
                {"error": "a session already exists for this meeting_url"},
                status_code=409,
            )
        try:
            result = await _start_avatar_session(
                req.meeting_url, req.avatar_id, req.join_at, integration,
                org_id=caller_org,
            )
        except recall_client.AvatarBusyError:
            return JSONResponse(
                {
                    "error": "avatar_busy",
                    "detail": "All avatars are busy right now — retry in a minute.",
                },
                status_code=503,
                headers={"Retry-After": "60"},
            )
        except entitlements.EntitlementsUnavailable:
            # Billing DB outage: fail CLOSED before any vendor dispatch —
            # never hand out unmetered paid minutes because Postgres blinked.
            return JSONResponse({"error": "billing_unavailable"}, status_code=503)
        except entitlements.UsageDenied as e:
            if e.reason == "active_session_exists":
                # One concurrent meeting per org (DB-enforced partial unique).
                return JSONResponse(
                    {"error": "active_session_exists"}, status_code=409
                )
            # Free allowance exhausted — the upgrade path (PR C) is the fix.
            return JSONResponse(
                {
                    "error": "usage_limit_reached",
                    "remaining_seconds": 0,
                    "checkout_path": "/billing/checkout",
                },
                status_code=402,
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    # Resolve any deploy-overlap duplicate the way the Gmail loop does, without
    # holding up the response.
    _schedule_start_reconcile(req.meeting_url, result["bot_id"])
    return JSONResponse(result)




# Telemetry-only (never gates behaviour): a loose "leave-ish word" check used to
# log that an ADDRESSED line looked like a dismissal but detect_leave_command
# didn't match — i.e. a phrasing we should probably add. Deliberately broad;
# it only ever feeds a PII-safe boolean log line.
_LEAVE_HINT = re.compile(
    r"\b(?:leave|exit|drop|hang up|disconnect|log ?o(?:ff|ut)|sign ?o(?:ff|ut)|"
    r"go(?:ne)?|dismiss|esci|uscire|vai|andare|abbandona|scollega|congeda)\b",
    re.IGNORECASE,
)


# Content-word dedup for actions. The LIVE path stores the raw spoken utterance
# ("send the rollout doc to Marco by Friday"); the summarizer re-extracts the
# SAME action but splits the deadline into its own field ("Send the rollout doc
# to Marco" + deadline="Friday"). Exact-text dedup misses that, so both survive
# with two action_ids — breaking the #84 dedup contract Cedric relies on.
# Comparing on content-token subset catches the rephrase.














# CEDRIC: live context push — the orchestrator POSTs a fresh brief the moment
# something changes (real-time counterpart of the periodic context pull);
# inject_brief re-reads per turn, so the next answer speaks from it.
@app.post("/sessions/{bot_id}/context")
async def push_context(
    bot_id: str, req: cedric.ContextPush, request: Request
) -> JSONResponse:
    caller_org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if caller_org is None:
        if err := cedric.auth_error(request):  # CEDRIC
            return err
    return cedric.apply_context_push(store.get(bot_id), req, caller_org)


@app.post("/sessions/{bot_id}/end")
async def end_session(bot_id: str, request: Request) -> JSONResponse:
    # Cookie (dashboard) or bearer (machine). auth.gate blocks an anonymous
    # caller from force-ending a bot (DoS + meter) on a login-protected
    # deployment. A logged-in user may end their own org's and unowned/service
    # sessions — never another org's. A PER-ORG machine bearer (org_tokens)
    # may end ONLY sessions of ITS org — never demo/unowned/another org's —
    # so a token that can start a session can also stop its meter (PR D
    # symmetry) without gaining the global bearer's reach.
    user = auth.current_user(request)
    token_org: Optional[str] = None
    if user is None:
        # Threadpooled: sync DB I/O (see _org_token_bearer_org's docstring).
        token_org = await run_in_threadpool(_org_token_bearer_org, request)
        if token_org is None:
            if err := auth.gate(request):
                return err
    else:
        live = store.get(bot_id)
        # A logged-in user may end their own org's and legacy unowned ("")
        # sessions. Demo-org sessions are NOT theirs — self-serve product
        # decision (2026-07-13): the Demo org is the anonymous showroom, and a
        # real signup must not be able to kill (or see) another visitor's demo.
        if live is not None and live.org_id not in ("", user["org_id"]):
            return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    if token_org is not None:
        live = store.get(bot_id)
        if live is not None and live.org_id != token_org:
            return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    artifact_scope = (
        token_org
        or (str(user["org_id"]) if user is not None else None)
    )
    artifact = await _finalize_session(
        bot_id, source="manual", artifact_org_id=artifact_scope
    )
    if artifact is None:
        # _finalize_session returns None only when the session is already gone
        # AND no artifact was stored — i.e. a genuinely unknown bot, OR a
        # concurrent terminal-webhook/reconcile finalize still in flight (its
        # artifact isn't saved until late in the body). Distinguish the two: a
        # bare 404 for a bot Cedric just had live is a misleading signal, so
        # answer 202 "finalizing" while another path owns it.
        if bot_id in _finalizing or store.get(bot_id) is not None:
            return JSONResponse({"ok": True, "finalizing": bot_id}, status_code=202)
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    artifact_org = str(artifact.get("org_id") or "")
    if user is not None and artifact_org not in ("", str(user["org_id"])):
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    if token_org is not None and artifact_org != token_org:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    return JSONResponse(cedric.wire_artifact(artifact))  # CEDRIC: PII stays home


@app.post("/sessions/{bot_id}/cancel")
async def cancel_session(bot_id: str, request: Request) -> JSONResponse:
    """Cancel a scheduled bot / abort a live one WITHOUT building an artifact.

    Used by the orchestrator when a calendar event moves or is cancelled (it
    rebooks afterwards). `end` keeps its meaning: finalize + artifact.
    """
    # PER-ORG machine bearers are first-class here (PR D): they authenticate
    # like the global bearer but may cancel ONLY their own org's sessions. The
    # global bearer keeps its full legacy service scope; key-free stays open.
    machine_org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if machine_org is None:
        if err := cedric.auth_error(request):  # CEDRIC
            return err
    session = store.get(bot_id)
    # Wrong-org answers the IDENTICAL body as not-found: a distinct 403 would
    # be an existence oracle (a per-org bearer probing whether another org's
    # bot_id exists). Adversarial review 2026-07-13, should-fix 2.
    if session is None or (
        machine_org is not None
        and session.org_id != machine_org
    ):
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    # Stop the meter: works for live bots; scheduled bots may reject leave_call,
    # so fall back to deleting the scheduled bot. Track whether the meter is
    # CONFIRMED off (leave succeeded, bot already gone 404/410, or the scheduled
    # bot was deleted) — PR B BLOCKER 2 gates the slot-release on that.
    meter_off = False
    try:
        await run_in_threadpool(recall_client.leave_call, bot_id)
        meter_off = True
    except Exception as e:  # noqa: BLE001 — classified by _leave_confirmed_stopped
        if _leave_confirmed_stopped(e):
            meter_off = True  # 404/410: bot genuinely gone → not billing
        else:
            try:
                await run_in_threadpool(recall_client.delete_bot, bot_id)
                meter_off = True  # a scheduled bot deleted → never billed
            except Exception as e2:  # noqa: BLE001 — surface but don't fail cancel
                print(f"[sessions] cancel: recall cleanup failed: {e2}", flush=True)
    if session.anam_conversation_id:
        try:
            await run_in_threadpool(
                anam_client.end_conversation, session.anam_conversation_id
            )
        except Exception:
            pass
    if not meter_off:
        # Meter-stop UNVERIFIED (Recall 5xx/network on BOTH leave and delete):
        # the bot may still be live+billing. Keep the session (leave_pending) so
        # the reconcile backstop retries the leave, and DO NOT close the usage
        # row / free the slot — a 2nd meeting for this org stays correctly
        # refused until the meter is confirmed off. _retry_leave closes the row.
        session.leave_pending = True
        session.usage_close_reason = "cancelled"
        session.usage_end_epoch = None
        print(
            f"[sessions] cancel: leave unverified — bot={bot_id} kept for "
            f"reconcile meter-stop retry",
            flush=True,
        )
        return JSONResponse(
            {"cancelled": False, "leave_pending": bot_id}, status_code=202
        )
    # PR B: meter confirmed off → release the usage row (a never-joined
    # scheduled bot closes 0; a live one closes with its elapsed) so the org
    # can book its next meeting.
    await _close_usage_for(session.org_id, bot_id, None, "cancelled")
    store.remove(bot_id)
    gpu_runtime.on_session_ended(len(store.all_sessions()))
    runpod_runtime.on_session_ended(len(store.all_sessions()))
    return JSONResponse({"cancelled": True, "bot_id": bot_id})


class DeliverRequest(BaseModel):
    to: list[str] = []
    slack: bool = True


@app.post("/sessions/{bot_id}/deliver")
async def deliver_artifact(bot_id: str, req: DeliverRequest, request: Request) -> JSONResponse:
    """Actually send the finished meeting's follow-up email + post it to Slack."""
    # Same machine-auth seam as /cancel: a PER-ORG bearer may deliver ONLY its
    # own org's artifacts; the global bearer keeps today's full service scope.
    machine_org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if machine_org is None:
        if err := cedric.auth_error(request):  # CEDRIC
            return err
    artifact = await run_in_threadpool(
        store.get_artifact, bot_id, org_id=machine_org
    )
    # Wrong-org == not-found, byte-identical (no existence oracle for per-org
    # bearers). Adversarial review 2026-07-13, should-fix 2.
    if artifact is None or (
        machine_org is not None
        and str(artifact.get("org_id") or "") != machine_org
    ):
        return JSONResponse({"error": "no artifact for this bot_id"}, status_code=404)

    # Finalize already ran store.remove(bot_id), so store.get(bot_id) is None here
    # and the live session no longer carries the avatar identity. The saved
    # artifact does (avatar_id stamped at finalize) — resolve the follow-up's
    # name from there first, so a Cedric meeting's Slack header reads "Cedric"
    # and not the default "Laura". Fall back to the default only when absent
    # (pre-finalize / legacy artifacts) or on an unknown id.
    avatar_id = artifact.get("avatar_id") or settings.default_avatar_id
    try:
        name = avatars.load(avatar_id).name
    except Exception:  # noqa: BLE001 — an unknown avatar id must never block delivery
        name = avatars.load(settings.default_avatar_id).name
    email = artifact.get("follow_up_email", {}) or {}

    email_res = await run_in_threadpool(
        actions.send_email, req.to, email.get("subject", ""), email.get("body", "")
    )
    slack_res = {"sent": False, "reason": "disabled"}
    if req.slack:
        # CAPABILITY GATE: Slack delivery happens ONLY when this avatar's `slack`
        # toggle is on. Read raw and skip on an explicit OFF — an untouched
        # avatar keeps today's behaviour (default on when the org connected
        # Slack via Cedric). avatar_id comes from the saved artifact above.
        caps = await run_in_threadpool(store.get_avatar_capabilities, avatar_id)
        if caps.get("slack") is False:
            slack_res = {"sent": False, "reason": "slack capability off"}
        else:
            slack_res = await run_in_threadpool(
                actions.post_to_slack, actions.artifact_to_slack_text(name, artifact)
            )
    return JSONResponse({"email": email_res, "slack": slack_res})






@app.get("/sessions/{bot_id}/artifact")
def session_artifact(bot_id: str, request: Request) -> JSONResponse:
    """Retrieve a finished session's artifact (summary + checklist + email)."""
    # A PER-ORG bearer may read ONLY its own org's sessions/artifacts. Another
    # org's bot_id — live OR finalized — answers the IDENTICAL not-found body,
    # so the endpoint is never an existence/progress oracle (adversarial
    # review 2026-07-13, should-fix 2). The global bearer and the key-free
    # demo keep today's full service scope.
    machine_org = cedric.resolve_machine_org(request)
    if machine_org is None:
        if err := cedric.auth_error(request):  # CEDRIC
            return err
    org_scoped = machine_org is not None
    live = store.get(bot_id)
    if live is not None:
        if org_scoped and live.org_id != machine_org:
            return JSONResponse({"error": "unknown bot_id"}, status_code=404)
        return JSONResponse({"status": "in_progress", "bot_id": bot_id})
    artifact = store.get_artifact(bot_id, org_id=machine_org)
    if artifact is None or (
        org_scoped and str(artifact.get("org_id") or "") != machine_org
    ):
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    return JSONResponse({"status": "done", **cedric.wire_artifact(artifact)})  # CEDRIC: PII stays home


@app.post("/sessions/{bot_id}/redeliver")
async def redeliver_artifact(bot_id: str, request: Request) -> JSONResponse:
    """Retry a finished session's signed ``session.ended`` callback.

    Callback delivery is deliberately best-effort during meeting cleanup, so
    the stored artifact is the recovery source of truth.  This endpoint gives
    a logged-in owner (or the machine bearer) a bounded retry without ever
    sending the transcript across the PII boundary. A PER-ORG bearer may
    redeliver ONLY its own org's artifacts (never demo/unowned/another org's).
    """
    user = auth.current_user(request)
    token_org: Optional[str] = None
    if user is None:
        # Threadpooled: sync DB I/O (see _org_token_bearer_org's docstring).
        token_org = await run_in_threadpool(_org_token_bearer_org, request)
        if token_org is None:
            if err := auth.gate(request):
                return err

    lookup_org = str(user["org_id"]) if user is not None else token_org
    artifact = await run_in_threadpool(
        store.get_artifact, bot_id, org_id=lookup_org
    )
    if artifact is None:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    artifact_org = str(artifact.get("org_id") or "")
    # Own-org + legacy unowned ("") only — demo-org artifacts excluded for
    # logged-in users (self-serve product decision, 2026-07-13; same rule as
    # /sessions/{id}/end and dashboard.visible).
    if user is not None and artifact_org not in ("", user["org_id"]):
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    if token_org is not None and artifact_org != token_org:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)

    integration = cedric.default_integration()
    if not integration or not integration.get("callback_url"):
        return JSONResponse(
            {"error": "orchestrator callback is not configured"}, status_code=503
        )
    integration = {**integration, "org_id": artifact_org}
    # send_ended retries up to 4x with BLOCKING time.sleep (5s + 25s + 120s), so
    # awaiting it inline hangs the request ~150s and 504s at the proxy. Hand it to
    # the SAME fire-and-forget seam finalize uses (cedric.deliver_ended: distils
    # via wire_artifact, then schedules the retrying send off the request) and
    # answer 202 immediately. Delivery semantics are unchanged (still the full
    # retry chain, still the distilled/no-transcript payload) — only the blocking
    # of the HTTP request is removed.
    cedric.deliver_ended(integration, bot_id, artifact)
    return JSONResponse({"status": "retrying", "bot_id": bot_id}, status_code=202)






# ───────────────────────── avatar page + ws ─────────────────────────


@app.websocket("/ws/{conversation_id}")
async def avatar_ws(websocket: WebSocket, conversation_id: str) -> None:
    await websocket.accept()
    session = store.get_by_conversation(conversation_id)
    if session is None:
        await websocket.close(code=4404)
        return
    session.ws = websocket
    try:
        while True:
            # The page may send heartbeats; we just keep the socket open.
            await websocket.receive_text()
    except WebSocketDisconnect:
        if session.ws is websocket:
            session.ws = None


@app.get("/avatar/stream/{conversation_id}")
async def avatar_stream(conversation_id: str) -> StreamingResponse:
    """SSE push channel for speak messages — the low-latency replacement for the
    500ms polling loop. App Runner rejects WebSocket upgrades at the edge but
    streams SSE fine (same mechanism as /live/ask), so queued messages are pushed
    within ~100ms instead of waiting out a poll interval. The page's EventSource
    auto-reconnects when App Runner recycles the request, and the 2s poll fallback
    below still catches anything in between — both drain the same queue, so a
    message is only ever delivered once.
    """

    async def gen():
        yield ": connected\n\n"
        last_beat = time.monotonic()
        while True:
            session = store.get_by_conversation(conversation_id)
            if session is not None:
                for msg in store.drain_avatar_messages(session):
                    yield f"data: {json.dumps(msg)}\n\n"
            if time.monotonic() - last_beat > 15:
                yield ": ping\n\n"  # keep-alive through proxies
                last_beat = time.monotonic()
            await asyncio.sleep(0.1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/avatar/messages/{conversation_id}")
def avatar_messages(conversation_id: str) -> JSONResponse:
    """HTTP fallback for platforms that block/strip WebSocket upgrades.

    AWS App Runner can reject WebSocket handshakes at the edge; Recall's browser
    can still fetch this endpoint, so the avatar page polls it and plays queued
    speak messages through Anam.
    """
    session = store.get_by_conversation(conversation_id)
    if session is None:
        return JSONResponse(
            {"messages": []},
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {"messages": store.drain_avatar_messages(session)},
        headers={"Cache-Control": "no-store"},
    )


class SpeakingReport(BaseModel):
    speaking: bool


@app.post("/avatar/speaking/{conversation_id}")
def avatar_speaking(conversation_id: str, rep: SpeakingReport) -> JSONResponse:
    """The avatar page reports its REAL speaking state (audio playing or queued).

    The backend's barge-in window is otherwise a words-per-second estimate that
    drifts both ways on long answers. True = heartbeat (extend the window a beat
    past the next report); false = she actually finished — close the window now,
    unless a speak was sent so recently the page may not have received it yet
    (keep a short grace so barge-in still covers the delivery gap)."""
    session = store.get_by_conversation(conversation_id)
    if session is None:
        return JSONResponse({"ok": False}, status_code=404)
    now = time.time()
    if rep.speaking:
        session.speaking_until = max(session.speaking_until, now + 1.6)
    elif now - session.last_spoke_at > 2.5:
        session.speaking_until = 0.0
    else:
        session.speaking_until = min(session.speaking_until, now + 2.5)
    return JSONResponse({"ok": True})


# ─────────────── rolling meeting notes (background refresh) ───────────────
_SUMMARY_KEEP_RECENT = 8    # lines the live history window already carries
_SUMMARY_EVERY_LINES = 20   # fold into the notes every this-many new lines
# Strong references to in-flight refresh tasks: the event loop holds tasks
# weakly, and a GC'd task would leave session.summarizing stuck True (notes
# frozen for the rest of the meeting).
_summary_tasks: set = set()


def _maybe_refresh_rolling_summary(
    session: store.Session, avatar: avatars.Avatar
) -> None:
    """Kick a background fold of older transcript lines into running notes.

    Fire-and-forget and self-throttling: at most one refresh in flight per
    session, only every _SUMMARY_EVERY_LINES lines, never on the stub (keyless
    demo). Zero cost on the live path — the model call runs in a worker thread.
    """
    if effective_provider() == "stub":
        return
    fresh = len(session.transcript) - session.summary_upto
    if fresh < _SUMMARY_EVERY_LINES + _SUMMARY_KEEP_RECENT or session.summarizing:
        return
    session.summarizing = True
    task = asyncio.create_task(_refresh_rolling_summary(session, avatar))
    _summary_tasks.add(task)
    task.add_done_callback(_summary_tasks.discard)


async def _refresh_rolling_summary(
    session: store.Session, avatar: avatars.Avatar
) -> None:
    try:
        cutoff = max(0, len(session.transcript) - _SUMMARY_KEEP_RECENT)
        lines = [
            u
            for u in session.transcript[session.summary_upto : cutoff]
            if u.speaker_kind != "agent"
        ]
        if not lines:
            # Progress the raw-transcript cursor even when this entire fold was
            # agent output, otherwise every future human line retries it.
            session.summary_upto = cutoff
            return
        text = "\n".join(f"{u.speaker}: {u.text}" for u in lines)
        notes = await run_in_threadpool(
            rolling_summary, avatar, session.rolling_summary, text
        )
        if notes:
            session.rolling_summary = notes
            session.summary_upto = cutoff
    except Exception:
        pass  # notes are a bonus — never let them disturb the live path
    finally:
        session.summarizing = False


# Fixed spoken furniture, in TWO languages: an English "let me think" in the
# middle of an Italian meeting breaks the illusion instantly. _line_for picks
# the pool matching what was just heard; the ANSWER language is the model's job.
_ACK_LINES = [
    "Mm-hm.",
    "Sure —",
    "On it.",
    "Let me think —",
    "Good one —",
    "Okay —",
    "Got it —",
]
_ACK_LINES_IT = [
    "Mm-hm.",
    "Certo —",
    "Subito.",
    "Vediamo —",
    "Arrivo —",
    "Ok —",
    "Ci penso io —",
]

# Ack for questions routed to the slower 'complex' Claude path: a line that
# JUSTIFIES the extra beat of latency instead of leaving it unexplained.
_THINK_LINES = [
    "Good question — give me a second to think it through.",
    "Let me reason through that for a moment.",
    "Hmm — let me think about that properly.",
    "Interesting one — give me a moment.",
    "Let me take a second on that.",
]
_THINK_LINES_IT = [
    "Bella domanda — dammi un secondo per pensarci.",
    "Fammi ragionare un attimo.",
    "Mmm — fammici pensare bene.",
    "Interessante — dammi un momento.",
    "Un secondo che ci ragiono.",
]

# Spoken when the answer stream drops AFTER the first token (a fast-provider
# blip that llm.stream_complete deliberately re-raises to avoid duplicate
# output). She has already started talking, so one honest recovery beat beats a
# 500 that cuts her off mid-sentence and makes Recall re-deliver. Fixed + short
# so they're TTS-prewarmed and land instantly.
_STREAM_RECOVERY_LINES = [
    "— sorry, I lost my train of thought there.",
    "— hmm, my thought dropped out there for a second. Give me a nudge?",
]
_STREAM_RECOVERY_LINES_IT = [
    "— scusa, ho perso il filo un attimo.",
    "— mmh, mi si è interrotto il pensiero. Rilanciatemi pure.",
]

# Confirmation for a captured action request (queue_action seam): promises
# follow-up after the call, never execution. Fixed lines so they're TTS-
# prewarmed — the confirmation must land as fast as an ack.
_QUEUE_LINES = [
    "Got it — I'll queue that for approval right after the call.",
    "Noted — I'll line that up for approval once we wrap.",
    "On it — it goes out for approval right after this meeting.",
]
_QUEUE_LINES_IT = [
    "Ricevuto — lo metto in coda per l'approvazione appena finiamo.",
    "Segnato — parte per l'approvazione subito dopo la call.",
]

# Voice-consent confirmations (settings.voice_consent_writes): the addressed
# ask was auto-approved and handed to Cedric to RUN now. Honesty rule intact —
# she says it's approved and underway, never that it's already done (the
# receipt lands on the dashboard when Cedric reports back).
_VOICE_LINES = [
    "On it — approved, and Cedric's running it now.",
    "Got it — that's approved and on its way through Cedric right now.",
    "Done — I've approved it and handed it to Cedric to run.",
]
_VOICE_LINES_IT = [
    "Subito — approvato, Cedric lo sta eseguendo ora.",
    "Ricevuto — approvato e già in lavorazione con Cedric.",
]

# Clarify-before-create (settings.clarify_before_create): the addressed ask is
# missing what a well-filed task needs, so the avatar asks ONCE — assembled
# from per-field slots, spoken via the normal TTS cache — and holds the
# approval until the asker replies (or the window lapses).
_CLARIFY_SLOTS = {
    "owner": "who should own it",
    "project": "which project it goes in",
    "due": "when it's due",
}
_CLARIFY_SLOTS_IT = {
    "owner": "chi la prende in carico",
    "project": "in quale progetto va",
    "due": "per quando serve",
}
_CLARIFY_WINDOW_S = 45.0  # after this, resolve quietly with what we have


def _clarify_line(heard: str, missing: list[str]) -> str:
    if sounds_italian(heard):
        slots = [_CLARIFY_SLOTS_IT[m] for m in missing if m in _CLARIFY_SLOTS_IT]
        joined = slots[0] if len(slots) == 1 else ", ".join(slots[:-1]) + " e " + slots[-1]
        return f"Certo — prima di crearla: {joined}?"
    slots = [_CLARIFY_SLOTS[m] for m in missing if m in _CLARIFY_SLOTS]
    joined = slots[0] if len(slots) == 1 else ", ".join(slots[:-1]) + ", and " + slots[-1]
    return f"Sure — before I create it: {joined}?"

# Listening cues spoken WHILE a human is mid-monologue (backchanneling, the
# thing that makes a listener feel present). Two syllables max — anything
# longer becomes an interruption instead of a nod.
_BACKCHANNEL_LINES = ["Mm-hm.", "Mm.", "Right."]
_BACKCHANNEL_LINES_IT = ["Mm-hm.", "Mm.", "Capito."]

# Meeting-chat line posted when she raises her hand (Recall has no raise-hand
# action, so the chat is the in-platform signal; the gesture on her /talk tile
# is the visual one). Tells the room HOW to give her the floor.
_HAND_CHAT_LINES = [
    '✋ {name} here — I have something to add when there\'s a moment. Just say "go ahead, {name}".',
    '✋ {name}: quick point on this when you have a second — just say "{name}, what\'s up?".',
]
_HAND_CHAT_LINES_IT = [
    '✋ {name}: avrei una cosa da aggiungere quando c\'è un attimo — basta dire "dimmi, {name}".',
    '✋ {name}: un appunto veloce su questo punto quando volete — dite "vai, {name}".',
]


def _avatar_voice(session: "store.Session") -> str:
    """The session avatar's ElevenLabs voice for TTS. avatars.load is
    mtime-cached (hot-path safe); any failure falls back to the global voice
    ("" keeps the shared prewarm cache key)."""
    try:
        return avatars.load(session.avatar_id).elevenlabs_voice_id or ""
    except Exception:  # noqa: BLE001
        return ""




def _should_backchannel(session: store.Session, text: str) -> bool:
    """A human is deep into a long utterance and she's been silent a while —
    one tiny cue ("Mm-hm.") reads as listening. Deliberately rare: long
    partials only, one per gap window, never while (or right after) she talks,
    so it stays a nod and never becomes chatter."""
    if not settings.backchannel_enabled:
        return False
    if len(text.split()) < settings.backchannel_min_words:
        return False
    now = time.time()
    if now < session.speaking_until:
        return False  # she's talking — that's not listening
    if now - session.last_backchannel_at < settings.backchannel_gap_seconds:
        return False
    if now - session.last_spoke_at < 12.0:
        return False  # just spoke/acked — another sound now reads as noise
    return True


# Spoken when dismissed by voice — short enough to finish inside
# settings.leave_grace_seconds before the bot disconnects.
_GOODBYE_LINES = [
    "Sure — bye everyone!",
    "Okay, leaving now. Bye!",
    "Got it — see you next time!",
    "Alright, I'll drop off. Bye!",
    "Thanks everyone — bye!",
    "Okay, heading out. Take care!",
]
_GOODBYE_LINES_IT = [
    "Certo — ciao a tutti!",
    "Va bene, esco. Ciao!",
    "D'accordo, vi lascio. A presto!",
    "Grazie a tutti — ciao!",
    "Perfetto, vado. Buon lavoro!",
]

# Footing (see docs/research/multiparty-meeting-intelligence.md): acknowledging
# people by name measurably drives liking and participation. {name} slots the
# joiner's / quiet participant's first name — dynamic, so never TTS-prewarmed.
_WELCOME_LINES = [
    "Hi {name}, welcome!",
    "Hey {name} — good to have you.",
    "Welcome, {name}!",
]
_WELCOME_LINES_IT = [
    # gender-neutral on purpose ("benvenuto/a" would have to guess)
    "Ciao {name}, che bello averti qui!",
    "Ciao {name} — piacere di averti qui.",
]
_QUIET_NUDGE_LINES = [
    "Before we close — {name}, anything from your side?",
    "One thing before we wrap up: {name}, anything you'd add?",
]
_QUIET_NUDGE_LINES_IT = [
    "Prima di chiudere — {name}, qualcosa da aggiungere?",
    "Un attimo prima di chiudere: {name}, tutto chiaro dal tuo lato?",
]

# Usage-deadline warnings (PR B): ONE short heads-up ~5 minutes before the
# included avatar time runs out, and one ~1 minute before she must leave.
# Spoken from the reconcile pass — never the live hot path. Each fires at most
# once per session (Session.usage_warned_5m / usage_warned_1m).
_USAGE_WARN_5M_LINES = [
    "Quick heads-up — about five minutes of included avatar time left for "
    "this workspace.",
    "Just so you know, this workspace has roughly five minutes of included "
    "time left.",
]
_USAGE_WARN_5M_LINES_IT = [
    "Un avviso veloce — restano circa cinque minuti di tempo incluso per "
    "questo workspace.",
    "Solo per informarvi: restano più o meno cinque minuti di tempo incluso.",
]
_USAGE_WARN_1M_LINES = [
    "We're at the last minute of included time — I'll have to leave shortly.",
    "One minute of included time left — I'll drop off in a moment.",
]
_USAGE_WARN_1M_LINES_IT = [
    "Siamo all'ultimo minuto di tempo incluso — tra poco dovrò uscire.",
    "Resta un minuto di tempo incluso — tra pochissimo dovrò lasciarvi.",
]


async def _usage_warn(session: store.Session, deadline: float) -> None:
    """Speak the ~5-min / ~1-min usage-deadline heads-up (config-gated via
    settings.usage_warnings_enabled). Reuses the proactive speak path
    (_make_avatar_speak honours the silent-notetaker gate and the repetition
    guard); each warning fires ONCE per session, skips when the timing is
    unsafe (too close to the cutoff to be useful), and never lets a failure
    escape into the reconcile pass."""
    if not settings.usage_warnings_enabled:
        return
    try:
        left = deadline - time.time()
        human = session.human_transcript()
        heard = human[-1].text if human else ""
        if left <= 60 and left > 5 and not session.usage_warned_1m:
            session.usage_warned_1m = True
            session.usage_warned_5m = True  # never follow with the milder one
            line = _line_for(heard, _USAGE_WARN_1M_LINES, _USAGE_WARN_1M_LINES_IT)
            await _make_avatar_speak(session, line, force=True)
        elif left <= 300 and left > 75 and not session.usage_warned_5m:
            session.usage_warned_5m = True
            line = _line_for(heard, _USAGE_WARN_5M_LINES, _USAGE_WARN_5M_LINES_IT)
            await _make_avatar_speak(session, line, force=True)
    except Exception:  # noqa: BLE001 — a warning must never crash the pass
        pass


# One-time self-introduction spoken shortly after join (settings.
# self_introduce_on_join). It breaks the "joined-but-mute" first impression
# WITHOUT breaking the etiquette: it names herself and tells the room how to
# call her in, then she goes back to waiting to be addressed. {name} slots the
# avatar's name — dynamic, so never TTS-prewarmed.
_SELF_INTRO_LINES = [
    "Hi, I'm {name} — here to help if you need me. Just say my name whenever "
    "you'd like me to jump in.",
    "Hey everyone, {name} here — I'll be listening. Say my name any time you "
    "want me to weigh in.",
]
_SELF_INTRO_LINES_IT = [
    "Ciao, sono {name} — sono qui se vi serve. Chiamatemi per nome quando "
    "volete che intervenga.",
    "Ciao a tutti, sono {name} — resto in ascolto. Ditemi il mio nome quando "
    "volete un mio contributo.",
]

_SPEECH_WORDS_PER_SECOND = 2.6  # ~ElevenLabs/edge-tts pace, for the barge-in window


async def _prewarm_tts_cache() -> None:
    """Pre-synthesize the fixed lines (acks, think lines, backchannels,
    goodbyes, search announces — both languages) into the TTS cache at boot.
    Sequential trickle, best-effort — vendor trouble here just means the live
    path warms lazily as before. Two consecutive misses = key/vendor trouble;
    stop burning boot-time calls.
    """
    warmed = 0
    misses = 0
    # One pass per DISTINCT avatar voice: "" (the global default) plus each
    # avatar.yaml override (e.g. cedric's Eric) — so his acks/confirmations are
    # as instant as Laura's.
    voices = {""}
    for aid in avatars.list_ids():
        try:
            voices.add(tts._norm_voice(avatars.load(aid).elevenlabs_voice_id or ""))
        except Exception:  # noqa: BLE001
            pass
    for line in (
        *_ACK_LINES,
        *_ACK_LINES_IT,
        *_THINK_LINES,
        *_THINK_LINES_IT,
        *_STREAM_RECOVERY_LINES,
        *_STREAM_RECOVERY_LINES_IT,
        *_QUEUE_LINES,
        *_QUEUE_LINES_IT,
        *_BACKCHANNEL_LINES,
        *_BACKCHANNEL_LINES_IT,
        *_GOODBYE_LINES,
        *_GOODBYE_LINES_IT,
        *SEARCH_ANNOUNCE_LINES,
    ):
        for voice in voices:
            try:
                if await tts.synthesize_cached(line, voice) is not None:
                    warmed += 1
                    misses = 0
                else:
                    misses += 1
            except Exception:  # noqa: BLE001
                misses += 1
        # Bail once ElevenLabs looks down for a whole line across all voices
        # (no key / outage) — no point paying the rest of the round-trips.
        if misses >= 2 * len(voices):
            break
    print(f"[startup] tts cache prewarmed: {warmed} fixed lines", flush=True)


def _norm_line(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def _is_repeat(session: store.Session, text: str) -> bool:
    """Repetition guard: True if this exact line was already spoken recently.
    Saying the same sentence twice in a couple of minutes is never useful — it
    reads as a glitch (looping repair lines, identical stub answers)."""
    norm = _norm_line(text)
    if not norm:
        return True
    now = time.time()
    window = settings.repeat_suppress_seconds
    recent = session._recent_lines
    # prune expired entries so the dict stays tiny
    for k in [k for k, ts in recent.items() if now - ts > window]:
        recent.pop(k, None)
    if norm in recent:
        return True
    recent[norm] = now
    return False


async def _make_avatar_speak(
    session: store.Session,
    text: str,
    citations: list | None = None,
    *,
    force: bool = False,
    generation: int | None = None,
    backchannel: bool = False,
    audio: dict | None = None,
    mood: str | None = None,
) -> bool:
    """Backend-as-brain: send the exact words for the avatar to speak (Anam talk).

    Returns False when the line was suppressed by the repetition guard.
    `force=True` bypasses the guard (someone re-asking Laura BY NAME deserves
    the answer again, even verbatim) while still refreshing the window. Also
    extends the estimated speaking window that powers barge-in.

    `generation` ties the line to one speech turn: if a stop (barge-in) or a
    newer turn bumped the session's generation while this sentence was still
    being generated, the line is dropped instead of un-muting her. Every
    message carries its generation_id so the page can drop a stale speak that
    raced a stop over the wire (additive — pages that don't know it ignore it).

    `backchannel=True` marks a listening cue ("Mm-hm." while a human talks):
    it must NOT refresh the speak cooldown — a backchannel is not a turn, and
    it must never suppress a real answer seconds later.

    `audio` is an optional server-synthesized voice payload (the
    tts.synthesize_cached shape: base64 mp3 + word timings). When present it
    rides along in the speak message and the page skips its whole /tts
    round-trip; pages that don't know the fields ignore them and POST /tts as
    before — the contract stays additive.
    """
    # Silent notetaker mode: this avatar never speaks during the meeting — it
    # only listens, tracks state, and delivers the artifact at the end. One gate
    # here suppresses EVERY spoken line (greeting, answers, interventions,
    # nudges, action-capture confirmations); transcript capture + MeetingState
    # tracking + finalize delivery are separate paths and keep working.
    try:
        if avatars.load(session.avatar_id).silent:
            return False
    except Exception:  # noqa: BLE001 — never let a config read mute the guard logic
        pass
    if generation is not None and generation != session.speech_generation:
        return False  # turn was cancelled while this sentence was in flight
    if _is_repeat(session, text) and not force:
        return False
    message = {
        "type": "speak",
        "text": text,
        "citations": citations or [],
        "generation_id": (
            generation if generation is not None else session.speech_generation
        ),
        # Per-sentence emotion for the face: the brain's own label when it gave
        # one (`mood`), else derived from the words. Additive — a page that
        # doesn't read it renders neutral as before. A backchannel ("Mm-hm.")
        # stays neutral: a listening cue shouldn't emote. See emotion.py.
        "emotion": emotion.DEFAULT if backchannel else emotion.normalize(
            mood if mood else emotion.classify(text)
        ),
    }
    if audio and audio.get("audio"):
        message["audio"] = audio["audio"]
        message["words"] = audio.get("words")
        message["wtimes"] = audio.get("wtimes")
        message["wdurations"] = audio.get("wdurations")
        message["engine"] = audio.get("engine")
    # Estimate how long this line keeps her talking; queued lines extend it.
    # With server-synthesized audio the REAL duration is known from the last
    # word's timings — barge-in stops estimating and starts knowing.
    est = max(1.0, len(text.split()) / _SPEECH_WORDS_PER_SECOND)
    if audio and audio.get("wtimes") and audio.get("wdurations"):
        est = max(1.0, (audio["wtimes"][-1] + audio["wdurations"][-1]) / 1000 + 0.3)
    session.speaking_until = max(session.speaking_until, time.time()) + est
    if session.ws is not None:
        try:
            await session.ws.send_json(message)
            if not backchannel:
                session.mark_spoke()
            return True
        except Exception as e:
            print(f"[avatar] websocket send failed; queued speak: {e}", flush=True)
            session.ws = None
    store.queue_avatar_message(session, message)
    if not backchannel:
        session.mark_spoke()
    print("[avatar] queued speak for HTTP polling", flush=True)
    return True


async def _speak_with_audio(
    session: store.Session,
    text: str,
    *,
    force: bool,
    generation: int,
    prev: "asyncio.Task | None",
    t0: float | None = None,
) -> bool:
    """Synthesize server-side, then speak — pipelined across sentences.

    Each sentence's synthesis runs CONCURRENTLY with the still-streaming
    answer (sentence N+1 generates while N synthesizes); awaiting `prev`
    before sending keeps the spoken order strict. Synthesis failure (or no
    ElevenLabs key) degrades to an audio-less speak — the page then does its
    own /tts with the edge fallback, exactly as before this feature.

    `t0` (set on the turn's first sentence) logs the wake->first_speak
    latency at the moment the first line is actually SENT.
    """
    payload = None
    try:
        if generation == session.speech_generation:
            payload = await tts.synthesize_cached(text, _avatar_voice(session))
    except Exception:  # noqa: BLE001 — synth is an optimization, never a blocker
        payload = None
    if prev is not None:
        try:
            await prev
        except Exception:  # noqa: BLE001 — a failed older send must not mute the rest
            pass
    if t0 is not None:
        print(
            f"[latency] wake->first_speak={(time.perf_counter() - t0) * 1000:.0f}ms",
            flush=True,
        )
    return await _make_avatar_speak(
        session, text, force=force, generation=generation, audio=payload
    )


async def _make_avatar_stop(session: store.Session) -> None:
    """Barge-in: tell the avatar page to stop the current speech immediately
    (TalkingHead cancels audio + queue). Same delivery contract as speak —
    additive message type; pages that don't know it ignore it.

    A stop kills the WHOLE turn, not just the audio playing right now, via
    three layers: queued-but-undelivered speaks are purged, the generation
    bump makes the still-streaming answer loop break (so she doesn't resume
    the old answer at the next sentence), and the stop carries the stale
    generation so the page drops any speak that raced it over the wire."""
    stale = session.speech_generation
    store.bump_speech_generation(session)
    store.purge_pending_speaks(session)
    session.speaking_until = 0.0
    message = {"type": "stop", "generation_id": stale}
    if session.ws is not None:
        try:
            await session.ws.send_json(message)
            return
        except Exception:
            session.ws = None
    store.queue_avatar_message(session, message)


async def _send_avatar_control(session: store.Session, message: dict) -> None:
    """Deliver a non-speech control message ({"type": ...}) to the avatar page.
    Same additive delivery contract as speak/stop — ws first, HTTP queue as the
    net; pages that don't know the type ignore it."""
    if session.ws is not None:
        try:
            await session.ws.send_json(message)
            return
        except Exception:
            session.ws = None
    store.queue_avatar_message(session, message)


async def _raise_hand(session: store.Session, avatar, heard: str = "") -> None:
    """Hand-raise etiquette: the room is talking among itself and she has a
    grounded contribution — instead of speaking over the conversation she
    raises her hand (gesture on her /talk tile) and posts one meeting-chat
    line saying how to give her the floor. The contribution itself waits in
    session.pending_contribution until someone invites her ("dimmi, Laura")."""
    session.hand_raised_at = time.time()
    # Motivation-gate bookkeeping (caller already passed should_raise_hand).
    session.hand_raise_count += 1
    session.hand_last_raise_at = session.hand_raised_at
    session.hand_last_contribution = session.pending_contribution
    await _send_avatar_control(session, {"type": "raise_hand"})
    # Chat line: genuinely fire-and-forget — Recall's read timeout is up to 60s,
    # so posting it inline could hold THIS webhook's response open on a slow/hung
    # chat endpoint. Detach it: a Recall hiccup (or the key-free demo, where there
    # is no real bot) must never block or delay the meeting.
    if settings.recall_api_key:
        line = _line_for(heard, _HAND_CHAT_LINES, _HAND_CHAT_LINES_IT).format(
            name=avatar.name
        )

        async def _post_hand_chat() -> None:
            try:
                await run_in_threadpool(
                    recall_client.send_chat_message, session.bot_id, line
                )
            except Exception as e:  # noqa: BLE001
                print(f"[hand] chat message failed (hand still raised): {e}", flush=True)

        asyncio.create_task(_post_hand_chat())


async def _lower_hand(session: store.Session) -> None:
    """Put the hand down and drop the queued contribution (delivered, answered
    another way, or the moment simply passed)."""
    session.hand_raised_at = 0.0
    session.pending_contribution = ""
    await _send_avatar_control(session, {"type": "lower_hand"})


def _is_own_speech(
    avatar_name: str,
    speaker: str,
    speaker_kind: str = "human",
) -> bool:
    """Own speech is an identity classification, never a name comparison.

    avatar_name/speaker remain in the signature for call-site compatibility;
    a real human is allowed to share the avatar's display name.
    """
    return speaker_kind == "agent"

def _in_opening_grace(session: store.Session) -> bool:
    """Opening settle-in ("wait to be called"): True while she should stay silent
    unless DIRECTLY addressed by name. Ends the instant she's first addressed
    (session.addressed_once) or after settings.opening_grace_seconds from join,
    whichever comes first — so she never talks over the room while it settles,
    but engages immediately when named and becomes proactive once things settle
    even if nobody names her.

    With first_call_required (the default) the grace never expires on its own:
    being named once is the ONLY thing that activates her — before that she is
    a silent guest, however long the meeting runs."""
    if session.addressed_once:
        return False
    if settings.first_call_required:
        return True
    if settings.opening_grace_seconds <= 0:
        return False
    return (time.time() - session.created_at) < settings.opening_grace_seconds


# Strong refs to in-flight self-intro tasks: a bare create_task is only weakly
# held by the loop and can be GC'd mid-sleep, silently killing the feature (same
# pattern as _summary_tasks above).
_self_intro_tasks: set = set()

# Recheck cadence while waiting for the floor to open before the self-intro.
_SELF_INTRO_RECHECK_SECONDS = 2.0


def _self_intro_already_active(session: store.Session) -> bool:
    """True when the meeting has already activated her (named her, or she
    already spoke/acked) — the self-introduction's whole job (tell the room how
    to call her in) is then moot, so it must NOT fire."""
    return session.addressed_once or session.last_spoke_at > 0


def _self_intro_floor_busy(session: store.Session) -> bool:
    """True when a human is audibly mid-utterance right now — introducing herself
    over them is the exact talk-over the etiquette avoids. Uses the same signal
    the interjection floor gate uses: a human partial landed within
    interject_min_pause_seconds (last_human_partial_at is stamped on EVERY human
    partial regardless of addressing)."""
    return (
        time.time() - session.last_human_partial_at
    ) < settings.interject_min_pause_seconds


def maybe_self_introduce(session: store.Session) -> bool:
    """One-time self-introduction on join (settings.self_introduce_on_join).

    First-call etiquette keeps her a SILENT guest until someone says her name,
    which on a first-time room (nobody knows to call her by name) leaves a
    joined-but-mute avatar with no cue how to activate her. Once per session,
    shortly after she is proven to be in the call (the FIRST transcript webhook,
    the same "proof the bot is in the call" trigger cedric.maybe_refresh_context
    uses), schedule ONE short spoken line introducing herself and telling the
    room how to call her in — then she goes back to waiting to be addressed.

    Fire-and-forget and OFF the live hot path: this does only flag checks; the
    delay + speak run inside a detached task. The one-shot flag is flipped BEFORE
    the task launches (no await between check and flip) so racing partial/final
    webhooks on the same event loop cannot double-launch. Returns True when a
    self-intro task was scheduled."""
    if not settings.self_introduce_on_join:
        return False
    if session.self_introduced:
        return False
    if _self_intro_already_active(session):
        # The meeting named her / she spoke before the first transcript we saw —
        # the intro is moot. Mark it done so we stop re-checking every webhook.
        session.self_introduced = True
        return False
    session.self_introduced = True  # flip first: intro schedules exactly once
    task = asyncio.create_task(_self_introduce_after_delay(session))
    _self_intro_tasks.add(task)  # strong ref so the task isn't GC'd mid-sleep
    task.add_done_callback(_self_intro_tasks.discard)
    return True


async def _self_introduce_after_delay(session: store.Session) -> None:
    """The delayed body behind maybe_self_introduce. Waits the settle-in delay,
    then introduces herself at the first OPEN floor — never over a human.

    Two guards, re-checked on every loop:
      - suppression: if the room activated her (named her or heard her speak),
        abort — the intro is now redundant;
      - talk-over: if a human is audibly mid-utterance, do NOT speak. Re-poll for
        a natural pause every _SELF_INTRO_RECHECK_SECONDS and introduce at the
        first open floor, up to self_introduce_max_wait_seconds — after which the
        moment has passed and she gives up silently.

    So she self-introduces at the first natural pause, never over a human, and
    never if the room engaged her first."""
    try:
        await asyncio.sleep(max(0.0, settings.self_introduce_after_seconds))
        deadline = time.time() + max(0.0, settings.self_introduce_max_wait_seconds)
        while _self_intro_floor_busy(session):
            # Someone is talking right now — don't barge in. Abort if she got
            # engaged meanwhile, or if the wait cap is reached (moment passed).
            if _self_intro_already_active(session) or time.time() >= deadline:
                return
            await asyncio.sleep(_SELF_INTRO_RECHECK_SECONDS)
    except asyncio.CancelledError:  # pragma: no cover — loop teardown
        return
    if _self_intro_already_active(session):
        return  # activated during the wait — the intro is now redundant
    # Session finalized/removed while she waited → don't speak into an orphaned
    # object (the meeting is over; the meter has stopped).
    if store.get(session.bot_id) is None:
        return
    try:
        avatar = avatars.load(session.avatar_id)
    except Exception:  # noqa: BLE001 — never let a config read crash a bg task
        return
    # Language follows the room if anything was heard, else defaults to English.
    heard = session.recent_transcript(3) if session.transcript else ""
    line = _line_for(heard, _SELF_INTRO_LINES, _SELF_INTRO_LINES_IT).format(
        name=avatar.name
    )
    # Normal speak path: _make_avatar_speak honours the silent-notetaker gate,
    # the repetition guard, and ws/HTTP delivery. force=True so the intro is
    # never dropped by the repeat guard.
    await _make_avatar_speak(session, line, force=True)


def _is_echo(session: store.Session, text: str) -> bool:
    """True when a 'human' line is actually HER OWN voice re-entering through a
    participant's mic (open speakers, no headphones): the transcribed text is a
    chunk of something she spoke seconds ago. Without this, her echo barges in
    on herself (she stops mid-answer 'on noise') and the final line even gets
    answered as if a human said it."""
    norm = _norm_line(text)
    if len(norm) < 12 or len(norm.split()) < 3:
        return False  # too short to attribute — leave it to the other gates
    now = time.time()
    recent = [
        spoken for spoken, ts in session._recent_lines.items() if now - ts < 45.0
    ]
    if any(norm in spoken for spoken in recent):
        return True
    # A Gemini-ears turn is ANOTHER model's transcription of her voice: the
    # wording/segmentation drifts from what she spoke, and one aggregated turn
    # can span several spoken lines — the substring test above misses both.
    # Token coverage catches it, but ONLY while an ears mode could actually be
    # feeding turns (off = Recall-only, where this branch is pure
    # false-positive risk against humans paraphrasing her). Two conditions
    # keep a human's confirmation/paraphrase alive: near-total coverage AND a
    # contiguous 4-word run she literally spoke — reordered paraphrases fail
    # the run test; a human's framing words ("so…", "…correct?") cut coverage.
    if settings.gemini_ears_mode.strip().lower() == "off":
        return False
    words = norm.split()
    if len(words) >= 5 and recent:
        vocab: set[str] = set()
        for spoken in recent:
            vocab.update(spoken.split())
        covered = sum(1 for w in words if w in vocab)
        if covered >= 0.85 * len(words) and _has_contiguous_run(words, recent):
            return True
    return False


def _has_contiguous_run(words: list[str], spoken_lines: list[str], n: int = 4) -> bool:
    """True when any contiguous n-word window of the turn appears verbatim
    inside one line she spoke — the signature of a re-transcription, which
    preserves word runs even when overall wording drifts."""
    if len(words) < n:
        return False
    grams = {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}
    return any(g in spoken for spoken in spoken_lines for g in grams)


# Partials made ONLY of filler/backchannel tokens ("yeah yeah", "uh uh ok",
# "sì sì va bene") — listening noises, never an interruption.
_FILLER_ONLY = re.compile(
    r"^(?:\s*(?:uh|um|mm+|hm+|eh|ah|oh|yeah|yep|yes|no|ok(?:ay)?|right|sure|"
    r"sì|si|già|va bene|bene|certo|ecco|beh|cioè|esatto|capito|giusto)"
    r"\b[\s,.!?]*)+$",
    re.IGNORECASE,
)


def _should_barge_in(
    session: store.Session,
    avatar_name: str,
    speaker: str,
    text: str,
    *,
    speaker_kind: str = "human",
) -> bool:
    """A human talked while Laura is (estimated) still speaking -> interrupt her.

    Not her own transcribed speech (the meeting bot hears her too), not her own
    ECHO through someone's open mic, and not filler/backchannels ("yeah yeah",
    "ok right") — those shouldn't cut her off.
    """
    if not settings.barge_in_enabled:
        return False
    if _is_own_speech(avatar_name, speaker, speaker_kind):
        return False
    if len(text.split()) < 3:
        return False
    if _FILLER_ONLY.match(text):
        return False
    if _is_echo(session, text):
        return False
    return time.time() < session.speaking_until


async def _ask_avatar_persona(session: store.Session, text: str) -> None:
    if session.ws is not None:
        await session.ws.send_json({"type": "ask", "text": text})
        session.mark_spoke()


# ─────────────────── calendar auto-join webhook ────────────────────
# Point a Recall calendar webhook (or your own calendar sync) at this endpoint.
# For each upcoming event that has a meeting link, we SCHEDULE Laura to join it.
# See docs/CALENDAR.md for the one-time OAuth setup. Payload shapes vary by
# provider, so parsing here is defensive — adjust `_extract_events` if needed.
def _extract_events(payload: dict) -> list[dict]:
    evs = (
        payload.get("events")
        or payload.get("data", {}).get("events")
        or ([payload.get("event")] if payload.get("event") else [])
        or ([payload.get("data")] if payload.get("data") else [])
    )
    return [e for e in evs if isinstance(e, dict)]


async def _calendar_events_from_payload(payload: dict) -> list[dict]:
    if payload.get("event") != "calendar.sync_events":
        return _extract_events(payload)

    data = payload.get("data", {})
    calendar_id = str(data.get("calendar_id") or "")
    if not calendar_id:
        return []
    updated_at_gte = str(data.get("last_updated_ts") or "")
    return await run_in_threadpool(
        lambda: recall_client.list_calendar_events(
            calendar_id=calendar_id,
            updated_at_gte=updated_at_gte,
        )
    )


def _calendar_event_id(event: dict) -> str:
    return str(
        event.get("id")
        or event.get("event_id")
        or event.get("calendar_event_id")
        or event.get("ical_uid")
        or ""
    )


def _calendar_event_start(event: dict) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    start = event.get("start_time") or event.get("start") or event.get("join_at")
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("date") or ""
    if not start and isinstance(raw, dict):
        raw_start = raw.get("start")
        if isinstance(raw_start, dict):
            start = raw_start.get("dateTime") or raw_start.get("date") or ""
    return str(start or "")


def _calendar_event_meeting_url(event: dict) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    conference = event.get("conference") or raw.get("conferenceData") or {}
    online_meeting = event.get("online_meeting") or event.get("onlineMeeting") or {}
    raw_online_meeting = raw.get("onlineMeeting") or {}

    url = (
        event.get("meeting_url")
        or event.get("meeting_link")
        or event.get("join_url")
        or (conference if isinstance(conference, dict) else {}).get("url")
        or (online_meeting if isinstance(online_meeting, dict) else {}).get("joinUrl")
        or (raw_online_meeting if isinstance(raw_online_meeting, dict) else {}).get("joinUrl")
        or (raw if isinstance(raw, dict) else {}).get("hangoutLink")
        or ""
    )
    if url:
        return str(url)

    entry_points = (conference if isinstance(conference, dict) else {}).get(
        "entryPoints", []
    )
    for entry in entry_points:
        if isinstance(entry, dict) and entry.get("uri"):
            return str(entry["uri"])
    return ""


def _emails_under(value: object) -> set[str]:
    emails: set[str] = set()
    if isinstance(value, str):
        return _split_emails(value)
    if isinstance(value, dict):
        for child in value.values():
            emails.update(_emails_under(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            emails.update(_emails_under(child))
    return emails


def _extract_invite_emails(event: dict) -> set[str]:
    """Extract attendee emails from provider/Recall calendar payload variants."""
    emails: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_norm = key.lower().replace("_", "").replace("-", "")
                is_attendee_field = (
                    key_norm in ATTENDEE_CONTAINER_KEYS
                    or key_norm.endswith("attendees")
                    or key_norm.endswith("participants")
                    or key_norm.endswith("invitees")
                    or key_norm.endswith("guests")
                    or key_norm.endswith("recipients")
                    or key_norm in {"attendeeemail", "participantemail", "inviteeemail"}
                )
                if is_attendee_field:
                    emails.update(_emails_under(child))
                elif isinstance(child, (dict, list, tuple)):
                    walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(event)
    return emails


def _calendar_event_targets_avatar(event: dict) -> bool:
    target_emails = _calendar_target_emails()
    if not target_emails:
        return True
    # Plus-aliases of a target inbox count as the inbox: an invite to
    # laura.ai.122222+cedric@gmail.com targets us (and names the avatar —
    # resolved separately via avatars.from_invite_email).
    def _base(addr: str) -> tuple[str, str]:
        base, _tag, domain = avatars.email_parts(addr)
        return base, domain

    targets = {_base(t) for t in target_emails}
    invited = {_base(e) for e in _extract_invite_emails(event)}
    return bool(invited & targets)


def _calendar_event_organizer_email(event: dict) -> str:
    """The organizer/creator address of a provider/Recall calendar event, ""
    when the payload variant doesn't carry one."""
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    for container in (event, raw):
        for key in ("organizer", "creator", "organizer_email", "organizerEmail"):
            emails = _emails_under(container.get(key))
            if emails:
                return sorted(emails)[0]
    return ""


def _org_for_calendar_event(event: dict) -> str:
    """Attribute a calendar-summoned meeting to the org that owns it.

    ORGANIZER-ONLY on purpose: the organizer's address resolves through
    store.org_for_email — since the personal-orgs cutover that is the
    organizer's own durable org. That org's meter runs and its tools act,
    not the Demo org's. Attendees never attribute: an external prospect's
    meeting that merely INVITES a registered user must not bill (or arm the
    tools of) that guest's org — cross-tenant mis-attribution is strictly
    worse than the Demo fallback.

    The organizer is TRUSTWORTHY here because it comes from Recall's sync of a
    connected Google Calendar (Google stamps the event creator) — unlike a raw
    mail header. The gmail-invite path deliberately does NOT attribute from the
    sender: Reply-To/From are unauthenticated (no SPF/DKIM on Reply-To) and the
    watcher reads one shared inbox, so a forged header could bill/arm a
    victim's org. That path stays on the Demo org until a per-user mailbox
    trust anchor exists (see docs/infra/ORG-ID-NAMESPACE-PERSONAL.md)."""
    def _base(addr: str) -> tuple[str, str]:
        base, _tag, domain = avatars.email_parts(addr)
        return base, domain

    organizer = _calendar_event_organizer_email(event).strip().lower()
    if not organizer:
        return settings.demo_org_id
    avatar_inboxes = {_base(t) for t in _calendar_target_emails()}
    if _base(organizer) in avatar_inboxes:
        return settings.demo_org_id
    org = store.org_for_email(organizer)
    if not org:
        return settings.demo_org_id
    if control_plane.enabled() and not control_plane.is_durable_org(org):
        return settings.demo_org_id
    return org


@app.post("/webhooks/recall-calendar")
async def recall_calendar_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    try:
        recall_client.verify_webhook(raw_body, request.headers)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=401)

    payload = json.loads(raw_body or b"{}")
    try:
        events = await _calendar_events_from_payload(payload)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    scheduled = []
    for ev in events:
        eid = _calendar_event_id(ev)
        url = _calendar_event_meeting_url(ev)
        start = _calendar_event_start(ev)
        # Which avatar: explicit field > plus-tagged invite address
        # (laura.ai.122222+cedric@… = Cedric's email) > the default.
        avatar_id = (
            ev.get("avatar_id")
            or avatars.from_invite_email(
                _extract_invite_emails(ev), _calendar_target_emails()
            )
            or settings.default_avatar_id
        )
        if not url or not start or (eid and store.is_scheduled(eid)):
            continue
        if not _calendar_event_targets_avatar(ev):
            scheduled.append({"event": eid, "skipped": "invite_email_missing"})
            continue
        # Durable cross-instance guard: don't add a second bot to a meeting that
        # already has one (deploy overlap, webhook retry, or Gmail path overlap).
        if await run_in_threadpool(_meeting_has_active_bot, url):
            if eid:
                store.mark_scheduled(eid)
            scheduled.append({"event": eid, "skipped": "already_has_bot"})
            continue
        try:
            avatar = avatars.load(avatar_id)
            conversation_id = uuid.uuid4().hex
            avatar_url = (
                f"{settings.public_base_url.rstrip('/')}/{avatar.page.strip('/')}"
                f"?avatar_id={avatar.id}&conversation_id={conversation_id}"
                f"&body={avatar.talk_body}&face_fallback={avatar.face_fallback}"
            )
            # Whose meeting is this? Attribute the dispatch to the owning org
            # (organizer/attendee → connected-Google/registered-user match) so
            # ITS meter runs and ITS tools act; Demo org only as the fallback.
            dispatch_org = await run_in_threadpool(_org_for_calendar_event, ev)
            # Entitlement gate (PR B): calendar auto-join takes this INLINED
            # dispatch path (not _start_avatar_session), so it must be gated
            # here too — every paid bot passes an open_usage gate. Same
            # provisional-id dance; a refusal marks the event skipped (fail
            # closed) and an EntitlementsUnavailable falls to the per-event
            # error handler below — either way no unmetered bot is born.
            usage_bot_id = ""
            if control_plane.enabled():
                usage_bot_id = f"pending:{uuid.uuid4().hex}"
                gate = await run_in_threadpool(
                    entitlements.open_usage,
                    dispatch_org, usage_bot_id, avatar.id,
                )
                if gate is not None and not gate.get("ok"):
                    scheduled.append(
                        {"event": eid, "skipped": gate.get("reason")}
                    )
                    continue
            try:
                bot = await run_in_threadpool(
                    recall_client.create_bot, url, avatar_url, start, avatar.name,
                    avatar.id,
                )
            except Exception:
                if usage_bot_id:  # release the slot — no bot was born
                    try:
                        await run_in_threadpool(
                            entitlements.close_usage,
                            dispatch_org, usage_bot_id, 0, "dispatch_failed",
                        )
                    except Exception:  # noqa: BLE001 — reconcile heals orphans
                        pass
                raise
            realtime_capability = str(
                bot.pop("_laura_realtime_capability", "") or ""
            )
            # Calendar auto-join has no authenticated principal (a webhook on
            # Laura's one Google account) — the owning org is resolved from the
            # event itself (_org_for_calendar_event); Demo org is the fallback.
            s = store.create(
                bot_id=bot["id"], meeting_url=url, avatar_id=avatar.id,
                org_id=dispatch_org,
            )
            if usage_bot_id and not await _assign_usage_bot_id(
                dispatch_org, usage_bot_id, bot["id"]
            ):
                await _finalize_session(
                    bot["id"], source="usage_binding_failed",
                    usage_reason="usage_binding_failed",
                )
                if store.get(bot["id"]) is None:
                    await run_in_threadpool(
                        entitlements.close_usage,
                        dispatch_org, usage_bot_id, 0,
                        "usage_binding_failed",
                    )
                raise entitlements.EntitlementsUnavailable(
                    "usage_bot_binding_failed"
                )
            if realtime_capability and not store.register_recall_realtime_capability(
                bot["id"], realtime_capability
            ):
                try:
                    await run_in_threadpool(recall_client.leave_call, bot["id"])
                finally:
                    store.remove(bot["id"])
                raise RuntimeError("could not secure Recall realtime endpoint")
            runpod_runtime.on_session_started(avatar.page)
            # CEDRIC: calendar-summoned (scheduled) bots take this inlined path,
            # NOT _start_avatar_session, so wire the Model A default here too —
            # otherwise a calendar invite bypasses the orchestrator exactly like
            # the email path did. None when no SURFACE_* is set.
            default_integ = cedric.default_integration()
            if default_integ:
                s.integration = {**default_integ, "org_id": dispatch_org}
            s.anam_conversation_id = conversation_id
            store.register_conversation(
                conversation_id, bot["id"], org_id=dispatch_org
            )
            if eid:
                store.mark_scheduled(eid)
            scheduled.append({"event": eid, "bot_id": bot["id"], "join_at": start})
        except Exception as e:  # one bad event shouldn't drop the webhook
            scheduled.append({"event": eid, "error": str(e)})
    return JSONResponse({"ok": True, "scheduled": scheduled})


# ── split-final leave helpers (see the voice-dismissal block below) ──
_ADDRESS_FILLERS = frozenset(
    {"hey", "ok", "okay", "hi", "yo", "hello", "so", "well", "um", "uh",
     "yeah", "please", "thanks"}
)


def _address_is_bare(avatar, text: str) -> bool:
    """True when an addressed turn is essentially JUST the name ("Cedric.",
    "hey Cedric") — the interrupted-dismissal shape that can split across ASR
    finals. A turn carrying real content ("Cedric, can you check the budget")
    is a complete utterance and must NOT arm the split-leave window, or a later
    same-speaker aside could end the meeting early (meter safety)."""
    wake = set(avatar.wake_words)
    residual = [
        t
        for t in re.findall(r"[a-z']+", text.lower())
        if t not in wake and t not in _ADDRESS_FILLERS
    ]
    # 0 residual tokens for an exact bare name; 1 for a fuzzy-corrupted name
    # (the mis-transcribed token isn't in wake_words) — both are "just the name".
    return len(residual) <= 1


def _names_another_participant(text: str, roster, wake_words) -> bool:
    """True when the line mentions ANOTHER participant's first name. A leave
    command in such a line ("Sara, you can leave") is plausibly aimed at that
    person, so the split-leave window must not read it as the avatar's own
    dismissal."""
    tokens = set(re.findall(r"[a-z']+", text.lower()))
    wake = set(wake_words)
    for name in roster:
        parts = name.strip().split()
        first = parts[0].lower() if parts else ""
        if first and first not in wake and first in tokens:
            return True
    return False


def _closing_signal(session: store.Session, text: str) -> bool:
    """Whether to treat THIS moment as the meeting wrapping up, for the two
    facilitation beats (proactive intervention + quiet-participant nudge).

    ``detect_closing``'s regex stays PRIMARY; this only ORs an additive fallback
    (decision.closing_fallback_fires) so the beats also fire on a natural lull
    with no exact closing phrase — the room went quiet for a while and someone
    just broke the silence. The current line is already appended to the
    transcript, so the previous line's timestamp is how long the room was idle
    before it. Both the regex and the fallback keep every downstream guard (the
    beats' one-shot flags, cooldown, confidence bar), so this can only ADD a
    trigger, never bypass a safety gate."""
    if detect_closing(text):
        return True
    human = session.human_transcript()
    prev_ts = human[-2].ts if len(human) >= 2 else 0.0
    return closing_fallback_fires(
        enabled=settings.closing_fallback_enabled,
        now=time.time(),
        meeting_start=session.created_at,
        last_line_at=prev_ts,
        idle_seconds=settings.closing_fallback_idle_seconds,
        min_meeting_seconds=settings.closing_fallback_min_meeting_seconds,
    )


def _capture_digest(*parts: object) -> str:
    canonical = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _recall_capture_identity(
    payload: dict,
    headers: object,
    *,
    signed: bool,
    org_id: str,
    bot_id: str,
    participant: dict,
    words: list,
    text: str,
) -> tuple[str, str]:
    """Build PII-free hashes that survive webhook retry and process restart.

    Recall's normalized final has no per-utterance id.  Its transcript id plus
    participant and relative word interval is the stable event identity.  A
    verified Svix/webhook id wins when present.  Legacy payloads without timing
    fall back to a bounded same-speaker/text fingerprint in the outbox DAL.
    """
    get_header = getattr(headers, "get", lambda _name, _default="": _default)
    webhook_id = ""
    if signed:
        webhook_id = str(
            get_header("webhook-id", "")
            or get_header("svix-id", "")
            or ""
        ).strip()
    data = payload.get("data") or {}
    transcript_id = str((data.get("transcript") or {}).get("id") or "")
    recording_id = str((data.get("recording") or {}).get("id") or "")
    endpoint_id = str((data.get("realtime_endpoint") or {}).get("id") or "")
    participant_id = str(participant.get("id") or participant.get("name") or "")
    normalized_text = " ".join((text or "").split()).casefold()

    def relative(word: dict, field: str) -> str:
        try:
            value = float(((word.get(field) or {}).get("relative")))
        except (TypeError, ValueError, AttributeError):
            return ""
        if not math.isfinite(value):
            return ""
        return format(value, ".6f")

    start = relative(words[0], "start_timestamp") if words else ""
    end = ""
    if words:
        end = relative(words[-1], "end_timestamp") or relative(
            words[-1], "start_timestamp"
        )

    event_key = ""
    if webhook_id:
        event_key = _capture_digest(
            "recall-webhook-v1", org_id, bot_id, webhook_id
        )
    elif transcript_id and start:
        event_key = _capture_digest(
            "recall-final-v1", org_id, bot_id, transcript_id,
            recording_id, endpoint_id, participant_id, start, end,
            normalized_text,
        )
    fingerprint = _capture_digest(
        "recall-final-fallback-v1", org_id, bot_id,
        participant_id, normalized_text,
    )
    return event_key, fingerprint


# ───────────────────────── recall webhook ──────────────────────────
@app.websocket("/realtime/recall-audio")
@app.websocket("/realtime/recall-audio/{cap_path}")
async def recall_audio_ws(websocket: WebSocket, cap_path: str = "") -> None:
    """Recall → us: the meeting's mixed raw audio for Gemini ears (flag-gated).

    Same trust model as /webhooks/recall: realtime endpoints are unsigned, so
    the URL carries the per-bot capability; an invalid/missing one is closed
    before any audio is read. Frames are JSON text messages whose payload is
    base64 s16le 16 kHz mono — forwarded verbatim to the bot's ears session.

    Path note: deliberately OUTSIDE /ws/ — the live-meeting contract route
    /ws/{conversation_id} is registered first and would capture any /ws/*
    path (including this one) as a conversation id.
    """
    # PII-safe observability: connection attempts are invisible in uvicorn's
    # access log (websockets aren't logged), so a silent no-show from Recall
    # and a rejected handshake would look identical without these prints.
    if not gemini_ears.enabled():
        print("[ears] audio ws attempt REJECTED: ears disabled", flush=True)
        await websocket.close(code=1008)
        return
    capability = (cap_path or websocket.query_params.get("cap") or "").strip()
    bot_id: str | None = None
    if capability:
        bot_id = await run_in_threadpool(
            store.resolve_recall_realtime_capability, capability
        )
    if not bot_id:
        print(
            f"[ears] audio ws attempt REJECTED: capability "
            f"{'missing' if not capability else 'unknown'}",
            flush=True,
        )
        await websocket.close(code=1008)
        return
    print(f"[ears] audio ws ACCEPTED bot={bot_id[:8]}", flush=True)
    await websocket.accept()
    # Persona for reply mode: the session's avatar name (best-effort).
    _ears_avatar = "Laura"
    _sess = store.get(bot_id)
    if _sess is not None:
        try:
            _ears_avatar = avatars.load(_sess.avatar_id).name
        except Exception:  # noqa: BLE001 — persona nicety, never block audio
            pass
    ears = gemini_ears.ensure_session(bot_id, capability, avatar_name=_ears_avatar)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            raw = message.get("text")
            if raw is None and message.get("bytes") is not None:
                raw = message["bytes"].decode("utf-8", "replace")
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if event.get("event") != "audio_mixed_raw.data":
                continue
            buffer_b64 = (
                ((event.get("data") or {}).get("data") or {}).get("buffer") or ""
            )
            if buffer_b64:
                ears.feed_audio(buffer_b64)
    except WebSocketDisconnect:
        pass
    # Recall reconnects on drops; the ears session survives to receive it.






@app.post("/webhooks/recall")
async def recall_webhook(request: Request) -> JSONResponse:
    # Recall realtime endpoints are unsigned. Production bot URLs therefore
    # carry a random per-session capability. Missing/wrong capabilities are
    # rejected before the body is read or parsed; the stored value is SHA-256
    # only. Svix-signed dashboard/status webhooks remain independently valid.
    signature_present = any(
        h in request.headers for h in ("webhook-signature", "svix-signature")
    )
    # An attacker can add a signature-looking header. It is an authentication
    # method only when the operator configured the verification secret; without
    # that secret the request remains unsigned and must present its capability.
    has_signature = signature_present and bool(
        settings.recall_webhook_secret.strip()
    )
    capability_bot_id: str | None = None
    if not has_signature:
        query_params = getattr(request, "query_params", {})
        capability = (query_params.get("cap") or "").strip()
        capability_required = bool(settings.recall_api_key.strip())
        if capability_required and not capability:
            return JSONResponse({"error": "missing realtime capability"}, status_code=401)
        if capability:
            capability_bot_id = await run_in_threadpool(
                store.resolve_recall_realtime_capability, capability
            )
            if capability_bot_id is None:
                return JSONResponse({"error": "invalid realtime capability"}, status_code=401)

    raw_body = await request.body()
    if has_signature:
        try:
            recall_client.verify_webhook(raw_body, request.headers)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=401)

    try:
        payload = json.loads(raw_body or b"{}")
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    # A valid capability authorizes exactly one bot. A payload claiming any
    # other session is rejected before transcript/roster/status side effects.
    if capability_bot_id is not None:
        claimed_bot_id = str(
            ((payload.get("data") or {}).get("bot") or {}).get("id") or ""
        )
        if not hmac.compare_digest(claimed_bot_id, capability_bot_id):
            return JSONResponse({"error": "capability/session mismatch"}, status_code=403)

    event = payload.get("event", "")

    if event == "transcript.partial_data":
        # Partials arrive WHILE someone is still talking; finals only land after
        # endpointing (~1-2s later). Two fluency wins here, but NO speak
        # decisions — answers, transcript, and MeetingState are finals-only
        # (partials repeat and get revised):
        #   - barge-in fires the instant a human talks over her
        #   - the ack fires the moment her name is heard, not when the
        #     sentence ends
        data = payload.get("data", {}).get("data", {})
        bot_id = payload.get("data", {}).get("bot", {}).get("id", "")
        session = store.get(bot_id)
        if session is None:
            return JSONResponse({"ok": True, "note": "no session"})
        # CEDRIC: fallback context pull — production (2026-07-10) shows the
        # realtime webhook often carries NO bot-status events (every finalize
        # arrived via reconcile), so handle_webhook_status's "live" trigger
        # never fires and the avatar sits in the meeting without Cedric's
        # brief. The first transcript IS proof the bot is in the call: launch
        # the same one-shot refresh here. Flag-guarded (runs once per session),
        # sync dict checks + create_task only — zero latency on the live path.
        cedric.maybe_refresh_context(session)
        # One-time self-introduction: the first transcript is proof she's in the
        # call — schedule the delayed intro off it (one-shot, non-blocking).
        maybe_self_introduce(session)
        words = data.get("words", [])
        text = " ".join(w.get("text", "") for w in words).strip()
        participant = data.get("participant") or {}
        identity = session.resolve_participant(
            participant.get("name"),
            participant.get("id"),
            metadata=participant,
        )
        speaker = identity["name"]
        speaker_kind = identity["kind"]
        if not text:
            return JSONResponse({"ok": True})
        avatar = avatars.load(session.avatar_id)
        if _should_barge_in(
            session,
            avatar.name,
            speaker,
            text,
            speaker_kind=speaker_kind,
        ):
            await _make_avatar_stop(session)
        if _is_own_speech(avatar.name, speaker, speaker_kind):
            return JSONResponse({"ok": True, "partial": True})
        # Her own echo through an open mic is not a human talking: it must not
        # ack, backchannel, or (via the stamp below) cancel a deference wait.
        if _is_echo(session, text):
            return JSONResponse({"ok": True, "partial": True, "echo": True})
        # A human is audibly talking right now — any deference window waiting
        # on the final-transcript path sees this and yields to them.
        session.last_human_partial_at = time.time()
        called, question = detect_wake(avatar, text, session.present_names(avatar.name))
        # "Laura, stop / aspetta / basta" — obey on the PARTIAL, before the
        # sentence even finalizes. Complements barge-in (which needs 3+ words):
        # a two-word "Laura stop" must cut her off instantly, not get answered.
        if called and detect_stop_command(question):
            await _make_avatar_stop(session)
            return JSONResponse({"ok": True, "partial": True, "stopped": True})
        acked = False
        if (
            settings.ack_enabled
            and called
            # Ack discipline: partials are noisy half-words, so the ack (an
            # audible "Sure —") needs the EXACT name — a fuzzy match on a
            # partial fragment must never make her speak. And wait until a
            # question is actually forming (3+ words): a bare "Laura…" pause
            # acked instantly reads as talking over the person.
            and len(text.split()) >= 3
            and detect_wake(avatar, text, session.present_names(avatar.name), fuzzy=False)[0]
            # Search questions are announced by the answer stream itself.
            and not wants_web_search(question or text)
            # One ack per turn: partial streams repeat the same growing text.
            and time.time() - session.last_ack_at > 6.0
        ):
            session.last_ack_at = time.time()
            line = (
                _line_for(question or text, _THINK_LINES, _THINK_LINES_IT)
                if wants_deep_thought(question or text)
                else _line_for(question or text, _ACK_LINES, _ACK_LINES_IT)
            )
            # cached_payload: attach the voice only if it's already synthesized
            # (prewarmed at boot) — an ack must never wait on a vendor call.
            acked = await _make_avatar_speak(
                session, line, force=True, audio=tts.cached_payload(line, _avatar_voice(session))
            )
        # ── backchanneling ──
        # Nobody called her, someone is deep into a long point: one tiny
        # "Mm-hm." makes her feel present in the room. Not a turn: it never
        # refreshes the cooldown, so a real question right after still answers.
        # Not before activation: a listening cue from an avatar nobody has
        # spoken to yet reads as eavesdropping, not presence.
        if (
            not called
            and not acked
            and not _in_opening_grace(session)
            and _should_backchannel(session, text)
        ):
            session.last_backchannel_at = time.time()
            bc = _line_for(text, _BACKCHANNEL_LINES, _BACKCHANNEL_LINES_IT)
            await _make_avatar_speak(
                session,
                bc,
                force=True,
                backchannel=True,
                audio=tts.cached_payload(bc, _avatar_voice(session)),
            )
            return JSONResponse({"ok": True, "partial": True, "backchannel": True})
        return JSONResponse({"ok": True, "partial": True, "acked": acked})

    if event in ("participant_events.join", "participant_events.leave"):
        # Live roster: who is in the room, INCLUDING people who never speak.
        # This is what lets her answer "how many are we?" and address people
        # by name, and what keeps her out of exchanges between two others.
        bot_id = payload.get("data", {}).get("bot", {}).get("id", "")
        session = store.get(bot_id)
        if session is not None:
            data = payload.get("data", {}).get("data", {})
            p = data.get("participant") or {}
            key = str(p.get("id")) if p.get("id") is not None else ""
            is_new = bool(key and key not in session.participants)
            identity = session.participant_event(
                p.get("name"),
                p.get("id"),
                here=(event == "participant_events.join"),
                metadata=p,
            )
            label = identity["name"]
            avatar = avatars.load(session.avatar_id)
            if identity["kind"] != "agent":
                # ── footing: greet a late joiner by name ──
                # Only when the meeting is genuinely underway (start-of-call
                # joins greet each other anyway), only for NEW named humans,
                # and never over her own voice. Cheap acknowledgment has an
                # outsized social payoff (research doc).
                if (
                    event == "participant_events.join"
                    and settings.greet_joiners
                    and is_new
                    and len(session.human_transcript()) >= 4
                    and not _in_opening_grace(session)  # not while the room settles
                    and not label.lower().startswith("guest")
                    and time.time() > session.speaking_until
                ):
                    heard = session.recent_transcript(3)
                    line = _line_for(heard, _WELCOME_LINES, _WELCOME_LINES_IT).format(
                        name=label.split()[0]
                    )
                    await _make_avatar_speak(session, line, force=True)
        return JSONResponse({"ok": True})

    if event != "transcript.data":
        # Auto end-of-meeting: when Recall reports the call is over / bot done,
        # finalize the session (stop billing on both vendors + build the artifact).
        # NOTE: terminal status events (bot.done / bot.call_ended / bot.fatal)
        # CANNOT ride the per-bot realtime webhook — Recall delivers them ONLY to
        # the account/dashboard (Svix) webhook, so point that at
        # PUBLIC_BASE_URL/webhooks/recall (see docs/infra/RECALL-WEBHOOK-SETUP.md).
        # The reconciliation loop (_reconcile_sessions_loop) is the backstop if
        # that webhook is mis-configured or a delivery is dropped.
        TERMINAL = {
            "done", "call_ended", "fatal",
            "bot.done", "bot.call_ended", "bot.fatal",
        }
        # Account status-change payloads carry the short code at data.data.code;
        # the realtime shape uses data.status.code / data.code. Read all three so
        # both the terminal check AND notify_failed see the real code.
        status_code = (
            payload.get("data", {}).get("status", {}).get("code")
            or payload.get("data", {}).get("data", {}).get("code")
            or payload.get("data", {}).get("code")
            or ""
        )
        term = event in TERMINAL or status_code in TERMINAL
        # Fatal join failure — pass it INTO finalize so the Cedric notify_failed
        # fires under the guard (once), not here (which would double-fire when the
        # reconcile poll also sees the fatal). Catch it from either the short code
        # or the bot.fatal event name.
        failed = status_code == "fatal" or event == "bot.fatal"
        bid = payload.get("data", {}).get("bot", {}).get("id", "") or payload.get(
            "data", {}
        ).get("bot_id", "")
        session = store.get(bid) if bid else None
        if term and session is not None:
            await _finalize_session(
                bid, source="webhook", failed_code="fatal" if failed else "",
                bot_terminal=True,  # terminal webhook → Recall meter already off
            )
            return JSONResponse({"ok": True, "finalized": bid})
        # CEDRIC: relay non-terminal join progress to the orchestrator + a
        # one-time meeting-brief refresh once the bot is actually in the call.
        await cedric.handle_webhook_status(session, bid, status_code)
        return JSONResponse({"ok": True, "ignored": event or status_code})

    data = payload.get("data", {}).get("data", {})
    bot_id = payload.get("data", {}).get("bot", {}).get("id", "")
    session = store.get(bot_id)
    if session is None:
        return JSONResponse({"ok": True, "note": "no session"})

    # CEDRIC: fallback context pull — see the partial path above. Finals cover
    # the (rare) delivery where the session's very first webhook is a final.
    cedric.maybe_refresh_context(session)
    # One-time self-introduction — same one-shot trigger as the partial path,
    # for the (rare) delivery whose very first webhook is a final.
    maybe_self_introduce(session)

    words = data.get("words", [])
    text = " ".join(w.get("text", "") for w in words).strip()
    participant = data.get("participant") or {}
    # ── Gemini ears RELAY turn: attribute the speaker here ──
    # The Cloudflare relay hears mixed audio and can't tell WHO spoke, so its
    # synthesized final arrives with no participant name. Attribute it from the
    # backend's own Recall-final ring (most recent human within the window);
    # fall back to a generic label so the turn still drives the pipeline. Also
    # mark the relay active (drives suppression/failover of raw Recall finals).
    if payload.get("laura_ears"):
        gemini_ears.note_relay_turn(bot_id)
        if not participant.get("name"):
            # Attribute to a REAL, already-known human — never invent a generic
            # name. A phantom "Partecipante" would show up as an extra roster
            # entry and (e.g.) trip the hand-raise threshold in a 1:1. Chain:
            # recent Recall-final speaker -> last known ring speaker (any age) ->
            # an existing roster human -> only then a generic placeholder.
            _existing = session.roster()
            attributed = (
                gemini_ears.attribute_speaker(bot_id)
                or gemini_ears.last_ring_speaker(bot_id)
                or (_existing[-1] if _existing else "")
            )
            if attributed:
                participant = {**participant, "name": attributed}
    identity = session.resolve_participant(
        participant.get("name"),
        participant.get("id"),
        metadata=participant,
    )
    speaker = identity["name"]
    speaker_id = identity["id"]
    speaker_kind = identity["kind"]
    if not text:
        return JSONResponse({"ok": True})

    # ── Gemini ears (PER-AVATAR; off = this block is dead code) ──
    # The brain is chosen per avatar from the dashboard (store.avatar_brain_mode),
    # falling back to the global default — so Laura can be Gemini while Cedric is
    # Cerebras, changed live with no redeploy.
    _ears_mode = gemini_ears.mode_for_avatar(session.avatar_id)
    if gemini_ears.mode_enabled(_ears_mode):
        # A RAW Recall final feeds the speaker ring (name only, no content) so
        # ears turns can be attributed; synthesized finals skip it (they ARE
        # the ears output re-entering the pipeline).
        if not payload.get("laura_ears"):
            gemini_ears.observe_recall_final(bot_id, speaker)
        # on/reply with an active relay: the synthesized final is the
        # authoritative utterance — suppress the raw one BEFORE it can enter
        # the transcript (no double lines, no double answers). The moment the
        # relay goes quiet this returns False and Recall drives again.
        if gemini_ears.should_suppress_recall_final(bot_id, payload, _ears_mode):
            # BUT let LEAVE / STOP commands through even when suppressed: they're
            # control commands that MUST be reliable, and Deepgram transcribes
            # command words ("go out of the meeting", "stop") far better than
            # Gemini's conversational STT (which garbles them). The downstream
            # leave/stop guards still decide whether to actually act — this only
            # stops the raw final from being dropped before they can see it.
            _ctrl = detect_leave_command(text) or detect_stop_command(text)
            if not _ctrl:
                return JSONResponse({"ok": True, "ears": "suppressed"})

    capture_event_key, capture_fingerprint = _recall_capture_identity(
        payload,
        request.headers,
        signed=has_signature,
        org_id=session.org_id,
        bot_id=bot_id,
        participant=participant,
        words=words,
        text=text,
    )

    # Her own voice re-entering through a participant's open mic: not a human
    # line. Keep it out of the transcript entirely.
    if _is_echo(session, text):
        return JSONResponse({"ok": True, "spoke": False, "reason": "echo"})

    # Agent speech may remain in the transcript for audit/presentation, but it
    # must exit before barge-in, MeetingState, actions, readiness or prompts.
    session.add_utterance(
        speaker,
        text,
        participant_id=speaker_id,
        speaker_kind=speaker_kind,
    )
    avatar = avatars.load(session.avatar_id)
    if _is_own_speech(avatar.name, speaker, speaker_kind):
        return JSONResponse({"ok": True, "spoke": False, "reason": "own speech"})

    # ── barge-in: never talk over a human ──
    if _should_barge_in(
        session,
        avatar.name,
        speaker,
        text,
        speaker_kind=speaker_kind,
    ):
        await _make_avatar_stop(session)

    # ── silent intelligence layer ──
    # Fold this line into the structured MeetingState (steps covered, decisions,
    # owners, deadlines, risks) BEFORE any speak decision. Pure regex — adds no
    # latency to the live path — and informs both the closing intervention below
    # and the post-meeting artifact.
    state = meeting_state.observe(
        session,
        avatar,
        speaker,
        text,
        participant_id=speaker_id,
        speaker_kind=speaker_kind,
    )

    # ── rolling meeting notes (background) ──
    # Every ~20 lines, fold the transcript older than the live history window
    # into short running notes (fast model, off the hot path) so her context
    # is the WHOLE meeting, not just the last 8 lines.
    _maybe_refresh_rolling_summary(session, avatar)

    # Cross-meeting memory: lazily (re)load after a process restart, scoped to
    # this session's org (never another tenant's open items in the live prompt).
    if session.memory_brief is None:
        session.memory_brief = await run_in_threadpool(
            ledger.carryover_brief, session.meeting_url, org_id=session.org_id
        )
    memory = session.memory_brief or ""
    memory = cedric.inject_brief(session, memory)  # CEDRIC: brief ahead of carryover

    # Per-meeting MISSION (admin objective): a per-session mission
    # (MeetingContext.mission) wins, else the avatar's default (avatar.yaml). "" =
    # no mission = today's behaviour exactly. Folded into the answer + closing
    # prompts as an instruction — never a gate, so turn-taking still owns WHEN.
    mission = cedric.resolve_mission(session) or avatar.mission

    # ── when-to-speak gate ──
    # By default (require_wake_word=False) she answers any grounded question; the
    # SKIP sentinel + cooldown keep her from interjecting on things she can't ground.
    # Computed HERE — before the closing/proactive block — so a DIRECTLY-ADDRESSED
    # turn never pays for the synchronous proactive model call: the fast answer
    # path owns that turn, and the wrap-up check only looks at the next UNADDRESSED
    # lull. (Without this, the idle closing-fallback would fire the proactive
    # retrieve+LLM in front of her first token on any "Laura, …?" after a pause.)
    called, question = detect_wake(avatar, text, session.present_names(avatar.name))

    # ── proactive intervention (fires once, as the meeting wraps up) ──
    if (
        settings.proactive_enabled
        and not called  # a direct ask owns its turn — never add proactive latency
        and not session.proactive_done
        and not _in_opening_grace(session)  # never activated → stays a silent guest
        and _closing_signal(session, text)
        and not session.in_cooldown(avatar.speak_cooldown_seconds)
    ):
        flag = await run_in_threadpool(
            proactive_flag,
            avatar,
            session.transcript_text(include_agents=False),
            state=state,
            memory=memory,
            mission=mission,
        )
        conf = float(flag.get("confidence", 0.0))
        if flag.get("should_speak") and flag.get("line") and conf >= settings.proactive_min_confidence:
            session.proactive_done = True
            cits = flag.get("citations", [])
            line = flag["line"] + (f" — per {cits[0]}" if cits else "")
            await _make_avatar_speak(session, line, cits)
            return JSONResponse({"ok": True, "spoke": True, "proactive": True, "line": line})

    # ── opening settle-in: wait to be called ──
    # For the first moments after joining she stays silent unless directly
    # addressed — the room is still settling (hellos, "can you hear me?", late
    # joiners) and an unprompted answer/greeting there reads as interrupting.
    # Being named ONCE wakes her for the rest of the meeting; otherwise the grace
    # expires on its own. Direct commands (stop/leave) are `called`-gated, so
    # they still work during the grace — this only suppresses UNADDRESSED speech.
    if called:
        session.addressed_once = True
    elif _in_opening_grace(session):
        return JSONResponse({"ok": True, "spoke": False, "reason": "opening grace"})

    # ── hand-raise timeout ──
    # Nobody invited her and the conversation moved on: put the hand down
    # silently and drop the queued point — delivering it minutes later would
    # derail the room worse than the interruption she avoided. The room's
    # silence is feedback: the next raise backs off (ignored_gap_seconds).
    if (
        session.hand_raised_at
        and time.time() - session.hand_raised_at > settings.hand_raise_timeout_seconds
    ):
        session.hand_last_ignored = True
        await _lower_hand(session)

    # ── clarify-before-create: the asker answers the avatar's question ──
    # A capture that was missing details parked here (session.pending_clarify)
    # while the avatar asked. The SAME speaker's next line resolves it: detail
    # text extends the durable capture, a skip proceeds as-is; either way the
    # approval (voice consent) fires only NOW. A lapsed window resolves
    # quietly and lets the current line flow through the normal pipeline.
    clarify = getattr(session, "pending_clarify", None)
    if clarify is not None:
        c_item, c_speaker, c_ts, c_missing, c_event_key, c_fingerprint = clarify
        if (capture_event_key and capture_event_key == c_event_key) or (
            not capture_event_key
            and capture_fingerprint
            and capture_fingerprint == c_fingerprint
        ):
            # Recall retry of the original ask — already captured + asked.
            return JSONResponse(
                {"ok": True, "spoke": False, "action_capture": True, "duplicate": True}
            )
        expired = time.time() - c_ts > _CLARIFY_WINDOW_S
        answered = (not called) and speaker_id == c_speaker and not expired
        if answered and time.time() - c_ts < 4.0 and is_capture_continuation(text):
            # A late ASR fragment of the ORIGINAL ask, not an answer: extend
            # and re-check what is still missing before (re)asking anything.
            try:
                c_item, c_extended = await run_in_threadpool(
                    tools.extend_action_once,
                    session,
                    c_item,
                    text,
                    source_event_key=capture_event_key,
                    source_fingerprint=capture_fingerprint,
                )
            except outbox.ActionCaptureClosed:
                session.pending_clarify = None
                return JSONResponse(
                    {"ok": True, "spoke": False, "capture_rejected": "meeting_finalizing"}
                )
            still_missing = tools.missing_action_details(c_item.get("action") or "")
            if still_missing:
                session.pending_clarify = (
                    c_item, c_speaker, c_ts, still_missing, c_event_key, c_fingerprint,
                )
                return JSONResponse(
                    {
                        "ok": True,
                        "spoke": False,
                        "capture_extended": True,
                        # A replayed older fragment dedupes durably — report it
                        # honestly so retries are visibly no-ops.
                        "duplicate": not c_extended,
                    }
                )
            answered = True  # the fragment completed the ask — resolve below
            text_is_details = False
        else:
            text_is_details = answered and not tools.is_detail_skip(text)
        if answered or expired:
            session.pending_clarify = None
            if text_is_details:
                try:
                    c_item, _ = await run_in_threadpool(
                        tools.extend_action_once,
                        session,
                        c_item,
                        text,
                        source_event_key=capture_event_key,
                        source_fingerprint=capture_fingerprint,
                    )
                except outbox.ActionCaptureClosed:
                    return JSONResponse(
                        {"ok": True, "spoke": False, "capture_rejected": "meeting_finalizing"}
                    )
            if settings.voice_consent_writes:
                asyncio.create_task(
                    run_in_threadpool(cedric.voice_approve, session, c_item)
                )
            if answered:
                clar_gen = store.bump_speech_generation(session)
                line = (
                    _line_for(text, _VOICE_LINES, _VOICE_LINES_IT)
                    if settings.voice_consent_writes
                    else _line_for(text, _QUEUE_LINES, _QUEUE_LINES_IT)
                )
                session.last_ack_at = time.time()
                spoke = await _make_avatar_speak(
                    session,
                    line,
                    force=True,
                    generation=clar_gen,
                    audio=tts.cached_payload(line, _avatar_voice(session)),
                )
                return JSONResponse(
                    {"ok": True, "spoke": bool(spoke), "action_capture": True, "clarified": True}
                )
            # expired: resolved silently; the current line continues below.

    # ── action-capture continuation ──
    # A same-speaker follow-up right after a captured action (and NOT a new
    # wake) extends the captured item's text, so the artifact/ledger get the
    # whole ask even when ASR split it across finals. Short window only, and
    # only when the follow-up actually READS as a continuation: a new sentence
    # said inside the window ("perfetto, direi che abbiamo finito il test" 3s
    # after the capture — live repro 2026-07-10) must not be glued onto the
    # card. A real ASR split picks up mid-phrase; is_capture_continuation
    # (decision.py) rejects acknowledgement openers and wrap-up lines.
    pending = getattr(session, "last_capture", None)
    if pending is not None:
        p_item, p_speaker, p_ts = pending[:3]
        p_event_key = pending[3] if len(pending) > 3 else ""
        p_fingerprint = pending[4] if len(pending) > 4 else ""
        same_source = bool(
            (capture_event_key and capture_event_key == p_event_key)
            or (
                not capture_event_key
                and capture_fingerprint
                and capture_fingerprint == p_fingerprint
            )
        )
        if same_source:
            # The 2xx for either the initial final or its continuation was
            # lost. Preserve the continuation window; clearing last_capture
            # here would make the genuinely next ASR fragment disappear.
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": False,
                    "action_capture": True,
                    "duplicate": True,
                }
            )
        if (
            not called
            and speaker_id == p_speaker
            and time.time() - p_ts < 4.0
            and is_capture_continuation(text)
        ):
            try:
                updated_item, extended = await run_in_threadpool(
                    tools.extend_action_once,
                    session,
                    p_item,
                    text,
                    source_event_key=capture_event_key,
                    source_fingerprint=capture_fingerprint,
                )
            except outbox.ActionCaptureClosed:
                session.last_capture = None
                return JSONResponse(
                    {
                        "ok": True,
                        "spoke": False,
                        "capture_rejected": "meeting_finalizing",
                    }
                )
            if extended:
                session.last_capture = (
                    updated_item,
                    p_speaker,
                    time.time(),
                    capture_event_key,
                    capture_fingerprint,
                )
            else:
                # An older ASR final can replay after a newer continuation.
                # Durable dedupe correctly rejects it; do not let that replay
                # roll the in-memory source identity backward or refresh the
                # four-second continuation window.
                session.last_capture = (
                    updated_item,
                    p_speaker,
                    p_ts,
                    p_event_key,
                    p_fingerprint,
                )
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": False,
                    "capture_extended": True,
                    "duplicate": not extended,
                }
            )
        session.last_capture = None

    # ── voice stop ("Laura, stop / aspetta") ──
    # A stop is a command, never a question: cut the current turn and answer
    # nothing. (The partial path usually catches it first; this is the net.)
    if called and detect_stop_command(question):
        await _make_avatar_stop(session)
        return JSONResponse({"ok": True, "spoke": False, "stopped": True})

    # ── voice dismissal ("Cedric, you can leave") ──
    # Addressed by name + an explicit leave command → say goodbye, then end the
    # session exactly like a natural meeting end: bot leaves the call, the
    # post-meeting artifact is built, billing stops on both vendors. The
    # goodbye is best-effort — leaving (= stopping the meter) must never be
    # blocked by a TTS hiccup.
    #
    # Split-final case: "Cedric." and "you can leave" often arrive as TWO ASR
    # finals — the first wakes, the second isn't a wake, so neither final alone
    # fires the dismissal and a background reconcile poll ends the meeting
    # late. Complete it: if the SAME speaker addressed the avatar in the last
    # few seconds (bare or substantive turn), re-check the leave command on the
    # follow-up (and on the concatenated finals for mid-phrase splits).
    #
    # Meter safety (an early leave kills a live paid meeting) is why the split
    # path stays guarded: it fires ONLY when the follow-up is a WHOLE-ASK leave
    # command from the SAME speaker who just addressed the avatar, and it
    # doesn't name another KNOWN participant (a dismissal like "Sara, you can
    # leave" is aimed at Sara, not the avatar). detect_leave_command's own
    # guards apply on top.
    #
    # Accepted narrow residual: an addressed turn followed within 8s by a
    # same-speaker dismissal aimed at someone the roster doesn't yet know (a
    # never-spoken participant) or at no one ("ok you can go now") still fires.
    # Closing it needs a leading-proper-noun heuristic on ASR-cased text, which
    # would also swallow the common real dismissal ("You can leave" — leading
    # capital, no name), so it's left as a documented trade-off, not a bug.
    leave_now = called and detect_leave_command(question)
    if settings.leave_on_command and not leave_now and not called:
        # ── Unambiguous dismissal that NAMES the meeting itself ──
        # "go out the meeting", "leave the meeting", "esci dalla riunione", "vai
        # fuori al meeting": a whole-ask leave IMPERATIVE whose explicit object
        # is the meeting/call/room is aimed at the bot even without its name — a
        # human dismisses another human BY NAME, never with a bare imperative to
        # the room. Fires in any room size and outside the split window (the
        # owner naturally says "vai fuori al meeting" with no name). Kept safe by
        # detect_leave_command_explicit (imperative-only, explicit object, no
        # 2nd-person permission) PLUS the addressee guard: never a dismissal that
        # names another participant.
        if detect_leave_command_explicit(text) and not _names_another_participant(
            text, session.roster(avatar.name), avatar.wake_words
        ):
            leave_now = True
        # ── 1:1 room: an unaddressed dismissal can only be aimed at the avatar ──
        # With a single human in the roster there is no other possible
        # addressee, so a whole-ask leave command fires without the name and
        # without the split window (live test 2026-07-10: the owner naturally
        # says "esci dal meeting" with no name, later than any window). The
        # addressee guard still applies — a name-led "Sara you can leave now"
        # never fires even here (the roster can undercount right after a
        # mid-meeting restart, when it reseeds from transcript speakers).
        if (
            not leave_now
            and len(session.roster(avatar.name)) <= 1
            and plausible_leave_followup(text)
            and detect_leave_command(text)
        ):
            leave_now = True
        addressed = getattr(session, "last_addressed", None)
        if not leave_now and addressed is not None:
            a_speaker, a_ts, a_text = addressed
            if (
                speaker_id == a_speaker
                and time.time() - a_ts < 8.0
                # Addressee guard: the follow-up must START like a command
                # aimed at the avatar ("you can…", "esci…") — a leading name
                # ("Sara you can leave now") is aimed at that person, known to
                # the roster or not.
                and plausible_leave_followup(text)
                # The follow-up alone is the usual shape ("esci dal meeting" /
                # "you can leave now" seconds after an addressed turn); the
                # concatenated form still catches a mid-phrase ASR split
                # ("Cedric, you can" + "leave the meeting").
                and (
                    detect_leave_command(text)
                    or detect_leave_command(f"{a_text} {text}")
                )
                and not _names_another_participant(
                    text, session.roster(avatar.name), avatar.wake_words
                )
            ):
                leave_now = True
    # ── leave telemetry (PII-safe: booleans only, never the words) ──
    # A live "didn't leave" report is undiagnosable from the logs today (the
    # transcript is PII and never logged). Log WHY a leave-shaped line failed:
    #  - called=False → the phrase was fine but nobody said her name and no
    #    split window was armed (wake-gate miss);
    #  - called=True + hint → she was addressed and the line contains a
    #    leave-ish word, but detect_leave_command didn't match (regex-miss
    #    candidate — the phrasing needs to be added).
    if settings.leave_on_command and not leave_now:
        if not called and detect_leave_command(text):
            # humans + lead expose WHICH guard blocked it: roster size (the
            # 1:1 rule), window arming, and the addressee guard's verdict on
            # the first token (that token is generic vocabulary, not PII).
            _lead = re.search(r"[a-zà-ú]+", text.lower())
            print(
                "[leave] leave phrase heard but not fired (called=False, "
                f"split_window_armed={getattr(session, 'last_addressed', None) is not None}, "
                f"humans={len(session.roster(avatar.name))}, "
                f"lead_ok={plausible_leave_followup(text)}, "
                f"lead={_lead.group(0) if _lead else ''!r})",
                flush=True,
            )
        elif called and _LEAVE_HINT.search(question):
            # The matched hint verb is one generic word (esci/leave/vai…), not
            # transcript content — enough to reproduce the regex miss offline.
            print(
                "[leave] addressed line has a leave-ish word but no leave match "
                f"(regex-miss candidate, hint={_LEAVE_HINT.search(question).group(0)!r})",
                flush=True,
            )
    # Arm the split window on EVERY addressed turn, bare or substantive
    # ("Cedric." / "Cedric, thanks for that"). Live testing (2026-07-10,
    # [leave] telemetry: called=False, split_window_armed=False) showed the
    # dismissal usually lands a few seconds AFTER a substantive addressed turn
    # — the old bare-only arming missed it. Meter safety holds because the
    # follow-up must still be a whole-ask leave command (see the guards above);
    # a new speaker or a stale (>8s) window clears the arm, so it can never
    # linger into unrelated speech.
    if called:
        session.last_addressed = (speaker_id, time.time(), text)
    elif getattr(session, "last_addressed", None) is not None and (
        speaker_id != session.last_addressed[0]
        or time.time() - session.last_addressed[1] >= 8.0
    ):
        session.last_addressed = None

    if settings.leave_on_command and leave_now:
        session.last_addressed = None
        try:
            goodbye = _line_for(question or text, _GOODBYE_LINES, _GOODBYE_LINES_IT)
            await _make_avatar_speak(
                session, goodbye, force=True, audio=tts.cached_payload(goodbye, _avatar_voice(session))
            )
            await asyncio.sleep(settings.leave_grace_seconds)
        except Exception:
            pass  # goodbye is best-effort
        finally:
            # finally, not just except: CancelledError (deploy/restart killing
            # this request mid-goodbye) is a BaseException and would otherwise
            # skip the finalize — leaving the bot in the call, meter running.
            await _finalize_session(bot_id)
        return JSONResponse(
            {"ok": True, "spoke": True, "left": True, "reason": "leave_command"}
        )

    # ── hand raised → invited to speak ──
    # Her hand is up and someone said her name. A bare address ("Laura?") or an
    # explicit invitation ("dimmi, Laura" / "go ahead") hands her the floor:
    # deliver the queued contribution. Any substantive ask instead ("Laura,
    # what's the budget?") answers on the normal path below — either way the
    # hand comes down now.
    if called and session.hand_raised_at:
        pending = session.pending_contribution
        session.hand_last_ignored = False  # the room engaged her: no back-off
        await _lower_hand(session)
        if pending and (detect_invite(question) or _address_is_bare(avatar, text)):
            turn_gen = store.bump_speech_generation(session)
            spoke = await _speak_with_audio(
                session, pending, force=True, generation=turn_gen, prev=None
            )
            return JSONResponse(
                {"ok": True, "spoke": bool(spoke), "hand_delivered": True}
            )

    # ── footing: quiet-participant nudge (fires once, at wrap-up) ──
    # She knows who is in the room (roster) and who has spoken (transcript).
    # As the meeting wraps up — and she wasn't addressed directly — invite ONE
    # silent participant in: the verbal analogue of turn-yielding gaze, which
    # no shipping meeting-AI does by voice (research doc). Placed after the
    # proactive intervention (critical process gaps win the wrap-up slot).
    if (
        settings.quiet_nudge_enabled
        and not called
        and not session.quiet_nudge_done
        and not _in_opening_grace(session)  # never activated → stays a silent guest
        and _closing_signal(session, text)
        and len(session.human_transcript()) >= 12
        and not session.in_cooldown(avatar.speak_cooldown_seconds)
    ):
        # Names are presentation only. Consume one spoken occurrence per
        # roster occurrence so two humans called Alex don't collapse together.
        spoken_names = [
            u.speaker.strip().lower() for u in session.human_transcript()
        ]
        quiet = []
        for name in session.roster(avatar.name):
            normalized = name.strip().lower()
            if normalized in spoken_names:
                spoken_names.remove(normalized)
            elif not normalized.startswith("guest"):
                quiet.append(name)
        if quiet:
            session.quiet_nudge_done = True
            nudge = _line_for(
                text, _QUIET_NUDGE_LINES, _QUIET_NUDGE_LINES_IT
            ).format(name=quiet[0].split()[0])
            await _make_avatar_speak(session, nudge, force=True)
            return JSONResponse({"ok": True, "spoke": True, "quiet_nudge": True})

    if settings.require_wake_word and not called:
        return JSONResponse({"ok": True, "spoke": False, "reason": "not called"})
    question = question or text  # no wake word → treat the whole utterance as the ask
    # ── multi-party turn-taking ──
    # A line aimed at ANOTHER participant by name ("Marco, can you take
    # this?") is their turn, not hers — even in no-wake-word mode. Her own
    # wake word wins (checked above): "Laura, tell Marco…" still answers.
    roster = session.roster(avatar.name)
    if not called and addressed_to_other(text, roster):
        return JSONResponse({"ok": True, "spoke": False, "reason": "addressed to other"})
    # ── engaged follow-up ──
    # She JUST spoke and someone asks a question without her name — in a live
    # conversation that's almost always a follow-up to HER answer ("and what
    # about the deadline?"). Dialogue context is a first-class addressee
    # signal (research doc), so it bypasses the cooldown and the deference
    # wait below. The in-stream SKIP gate still protects the misfires.
    followup = (
        not called
        and settings.followup_window_seconds > 0
        and (time.time() - session.last_spoke_at) < settings.followup_window_seconds
        and text.rstrip().endswith("?")
    )

    # Cooldown throttles UNPROMPTED interjections. Being addressed by name is a
    # direct ask — follow-ups right after her answer are what a fluent
    # conversation is made of, so `called` (and `followup`) bypass it.
    if not called and not followup and session.in_cooldown(avatar.speak_cooldown_seconds):
        return JSONResponse({"ok": True, "spoke": False, "reason": "cooldown"})

    # While her hand is up, unaddressed talk is the room continuing without
    # her: don't generate a second contribution — the first one is already
    # queued and waiting for the invite (or the timeout above).
    if not called and session.hand_raised_at:
        return JSONResponse({"ok": True, "spoke": False, "reason": "hand raised"})

    # ── deference window ──
    # Nobody addressed her by name, so this is at best a room-open question:
    # humans get first right of reply. Wait briefly; if anyone starts talking
    # (a partial lands or the transcript grows), yield silently. Deliberately
    # AFTER the cheap gates — a line that would be skipped anyway never waits.
    if not called and not followup and settings.deference_seconds > 0:
        _defer_mark = len(session.human_transcript())
        _defer_t0 = time.time()
        # Size ONLY the wait — the yield decision below is unchanged. Adaptation
        # is off by default (returns deference_seconds verbatim).
        _defer_wait = adaptive_deference_seconds(
            settings.deference_seconds,
            enabled=settings.deference_adaptive_enabled,
            lo=settings.deference_min_seconds,
            hi=settings.deference_max_seconds,
            since_partial=_defer_t0 - session.last_human_partial_at,
            active_partial_seconds=settings.deference_active_partial_seconds,
            n_humans=len(roster),  # roster excludes the avatar → humans only
            is_question=text.rstrip().endswith("?"),
            # Does the line SOUND finished? "I wanted to ask about the…" waits
            # long (speaker mid-thought); "What's the deadline?" answers fast.
            turn_completeness=end_of_turn.completeness(text),
        )
        # Cross-talk: if two humans are in a tight back-and-forth right now, they
        # own the floor — wait the MAX rather than the sized window so she never
        # clips their volley. Suppression-only (still just a wait).
        if settings.cross_talk_suppression_enabled and in_locked_dyad(
            session.human_transcript(),
            avatar_name=avatar.name,
            now=_defer_t0,
            min_turns=settings.cross_talk_min_turns,
            max_gap_seconds=settings.cross_talk_max_gap_seconds,
            window=settings.cross_talk_window,
        ):
            _defer_wait = max(_defer_wait, settings.deference_max_seconds)
        await asyncio.sleep(_defer_wait)
        if (
            len(session.human_transcript()) > _defer_mark
            or session.last_human_partial_at > _defer_t0
        ):
            return JSONResponse(
                {"ok": True, "spoke": False, "reason": "deferred to human"}
            )

    # This is a NEW speech turn: bump the generation so an older turn that is
    # still streaming (slow model, long answer) stops queueing sentences under
    # her — its loop sees the newer generation and breaks. Barge-in stops
    # already bumped; this covers the no-audio overlap case (she was silent but
    # a previous answer was still generating).
    turn_gen = store.bump_speech_generation(session)

    # ── action requests: capture, never execute (queue_action platform seam) ──
    # "Cedric, can you send the recap?" is a request to DO something. Capture is
    # DETERMINISTIC — no LLM call, no tool loop, nothing slower than an ack: the
    # utterance itself becomes the queued action (the finalize summarizer and
    # the orchestrator's approval card refine it), the spoken confirmation is a
    # fixed line (cached TTS ⇒ instant), and action.requested fires OFF the
    # live path for orchestrated sessions. wants_action_capture is deliberately
    # narrow — content questions ("can you check if…") stay on the streamed
    # path, and search intents keep their announced streamed answer. Placed
    # BEFORE the generic ack: this confirmation IS the reply for the turn.
    # Only when addressed by name: an unaddressed "someone should send X" is
    # the summarizer's job at finalize.
    if called and wants_action_capture(question) and not wants_web_search(question):
        # detect_wake already stripped the wake word: `question` is the ask
        # itself ("please schedule a follow-up with Marco on Friday").
        # One bounded tenant transaction, off the shared event loop. No
        # callback network occurs on the live transcript path.
        try:
            item, created = await run_in_threadpool(
                tools.capture_action_once,
                session,
                question.strip(),
                source_event_key=capture_event_key,
                source_fingerprint=capture_fingerprint,
            )
        except outbox.ActionCaptureClosed:
            session.last_capture = None
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": False,
                    "capture_rejected": "meeting_finalizing",
                }
            )
        if not created:
            # A restart may have dropped the in-memory continuation window.
            # Re-arm it from the canonical durable row so the next genuine ASR
            # fragment is not lost after this initial-final replay.
            session.last_capture = (
                item,
                speaker_id,
                time.time(),
                capture_event_key,
                capture_fingerprint,
            )
            # Recall retry after a lost 2xx: the original durable action and
            # callback already own the acknowledgement. Never speak/kick twice.
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": False,
                    "action_capture": True,
                    "duplicate": True,
                }
            )
        # ASR often splits one ask across finals ("Cedric, can you send" +
        # "the recap by Friday"). Remember this capture so a same-speaker
        # follow-up within a few seconds extends its text (see the
        # continuation check after wake detection).
        session.last_capture = (
            item,
            speaker_id,
            time.time(),
            capture_event_key,
            capture_fingerprint,
        )
        if session.speech_generation != turn_gen:  # barge-in since the final landed
            return JSONResponse({"ok": True, "spoke": False, "interrupted": True})
        # A NEW addressed ask while an older clarify was still pending:
        # resolve the old capture as-is first (approve quietly with what it
        # has) so it is never lost, then handle this one on its own merits.
        stale = getattr(session, "pending_clarify", None)
        if stale is not None:
            session.pending_clarify = None
            if settings.voice_consent_writes:
                asyncio.create_task(
                    run_in_threadpool(cedric.voice_approve, session, stale[0])
                )
        missing = tools.missing_action_details(item.get("action") or "")
        if settings.clarify_before_create and missing:
            # The ask lacks what a well-filed task needs — Petra ASKS instead
            # of filing an orphan. The approval is held until the asker's
            # reply resolves it (clarify block above), or the window lapses.
            session.pending_clarify = (
                item, speaker_id, time.time(), missing,
                capture_event_key, capture_fingerprint,
            )
            line = _clarify_line(question, missing)
            session.last_ack_at = time.time()
            spoke = await _make_avatar_speak(
                session,
                line,
                force=True,
                generation=turn_gen,
                audio=tts.cached_payload(line, _avatar_voice(session)),
            )
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": bool(spoke),
                    "action_capture": True,
                    "clarifying": missing,
                }
            )
        if settings.voice_consent_writes:
            # CEDRIC voice consent: the addressed ask IS the approval — record
            # it on the canonical channel and tell Cedric to run it NOW, off
            # the live path. The spoken line says approved-and-running.
            asyncio.create_task(
                run_in_threadpool(cedric.voice_approve, session, item)
            )
            line = _line_for(question, _VOICE_LINES, _VOICE_LINES_IT)
        else:
            line = _line_for(question, _QUEUE_LINES, _QUEUE_LINES_IT)
        session.last_ack_at = time.time()  # the confirmation doubles as the ack
        spoke = await _make_avatar_speak(
            session,
            line,
            force=True,
            generation=turn_gen,
            audio=tts.cached_payload(line, _avatar_voice(session)),
        )
        return JSONResponse({"ok": True, "spoke": bool(spoke), "action_capture": True})

    # ── instant acknowledgment ──
    # She was addressed BY NAME, so she will answer — say so immediately while
    # the model generates. Sub-second social feedback is what makes the
    # conversation feel fluent instead of laggy. Slow routes get a line that
    # justifies their pause; search questions are announced by the stream
    # itself (brain._SEARCH_ANNOUNCE), so no ack here — she'd say two openers.
    # Usually the partial-transcript path already acked this turn (it hears the
    # name ~1-2s before this final lands) — last_ack_at dedupes the two paths.
    if (
        settings.ack_enabled
        and called
        and not wants_web_search(question)
        and time.time() - session.last_ack_at > 6.0
    ):
        session.last_ack_at = time.time()
        line = (
            _line_for(question, _THINK_LINES, _THINK_LINES_IT)
            if wants_deep_thought(question)
            else _line_for(question, _ACK_LINES, _ACK_LINES_IT)
        )
        await _make_avatar_speak(
            session,
            line,
            force=True,
            generation=turn_gen,
            audio=tts.cached_payload(line, _avatar_voice(session)),
        )

    # Backend is the brain: answer from OUR knowledge (RAG) with the recent
    # meeting conversation as context, plus the silent MeetingState tracker and
    # the rolling notes of everything older than the history window — so "what
    # did we decide / who owns X / what's missing?" answers from what she
    # actually tracked. Streamed sentence-by-sentence so the avatar starts
    # speaking on the first sentence instead of waiting for the whole answer.
    # Grounding is enforced by the SKIP sentinel inside the stream: if the
    # context is insufficient the generator yields nothing and the avatar stays
    # silent (the streaming equivalent of the old confidence gate).
    history = session.recent_transcript(n=8)
    # ── tutto-Gemini (GEMINI_EARS_MODE=reply) ──
    # The ears session already DRAFTED the spoken reply from the live audio
    # (same Live model that closed the turn — the draft streamed while the
    # human was finishing, so it costs zero extra model latency). When the
    # synthesized final carries it, speak THAT instead of calling the brain.
    # Every gate above already ran (wake, cooldown, deference, hand-raise);
    # ElevenLabs still speaks. Trade-off, by design for the A/B: the draft is
    # NOT grounded in the avatar's documents.
    ears_reply = ""
    if payload.get("laura_ears") and _ears_mode == "reply":
        ears_reply = str(payload.get("laura_ears_reply") or "").strip()
    _t_wake = time.perf_counter()
    spoke_any = False
    suppressed_any = False
    interrupted = False
    speak_tasks: list[asyncio.Task] = []
    prev_task: asyncio.Task | None = None
    # ── hand-raise mode ──
    # Nobody addressed her and the room is a multi-human conversation: whatever
    # grounded contribution the stream produces is QUEUED behind a raised hand
    # instead of spoken over the talk (the SKIP sentinel still applies — no
    # contribution, no hand). 1:1 meetings keep today's direct answers.
    hand_mode = (
        settings.hand_raise_enabled
        and not called
        and not followup
        and len(roster) >= settings.hand_raise_min_humans
    )
    hand_sentences: list[str] = []
    # Grounding confidence the retrieval already computed (top surviving chunk
    # score), read back after the stream ends to gate the interjection escape —
    # no second model call. Only consulted on the hand-raise path below.
    _answer_meta: dict = {}
    # Talk-over guard baseline (interject_recheck_floor_at_speak): snapshot the
    # transcript length and the turn-start clock BEFORE generation, so the
    # interjection floor check below can tell whether a human took the floor while
    # she was generating (a new final landed, or a human partial arrived during
    # the generation window). Cheap ints — no latency on the hot path.
    _interject_len0 = len(session.human_transcript())
    _interject_t0 = time.time()
    try:
        async for sentence in iterate_in_threadpool(
            # reply mode with a draft on board: the Gemini draft IS the answer
            # stream (single sentence-burst); otherwise the grounded brain.
            iter([ears_reply])
            if ears_reply
            else answer_question_stream(
                avatar,
                question or text,
                history=history,
                memory=memory,
                state=state,
                summary=session.rolling_summary,
                speaker=speaker,
                roster=roster,
                k=4,  # leaner context: input tokens ARE first-token latency live
                min_chars=45,  # coalesce tiny fragments so the TTS voice flows
                meta=_answer_meta,
                mission=mission,
                org_id=session.org_id,  # org's private docs join retrieval
            )
        ):
            # Interrupted (barge-in) or superseded by a newer turn while this
            # sentence was generating: abandon the rest of the answer. The page
            # already dropped the stale generation; don't keep paying for tokens.
            if session.speech_generation != turn_gen:
                interrupted = True
                break
            if hand_mode:
                hand_sentences.append(sentence)
                continue
            # Called by name -> answer even if it repeats a recent line; an
            # unaddressed duplicate is suppressed (and reported honestly below).
            # Speaking is pipelined: sentence N synthesizes server-side while
            # N+1 is still generating; the prev-chain keeps the spoken order.
            prev_task = asyncio.create_task(
                _speak_with_audio(
                    session,
                    sentence,
                    force=called,
                    generation=turn_gen,
                    prev=prev_task,
                    t0=_t_wake if not speak_tasks else None,
                )
            )
            speak_tasks.append(prev_task)
    except Exception as e:  # noqa: BLE001 — a mid-stream provider drop must not 500
        # llm.stream_complete deliberately RE-RAISES a fast-provider error that
        # lands AFTER the first token (a pre-token failure is already covered by
        # its Haiku fallback), so a Cerebras/Groq blip mid-answer arrives here.
        # If she has ALREADY started speaking, do NOT let it 500: Recall would
        # re-deliver the turn and she'd be cut off mid-sentence. Finish the
        # sentences in flight, say ONE short recovery line on the SAME speak path
        # the loop uses, and return 200 so Recall does not re-deliver.
        #
        # Nothing spoken yet (no speak task created) OR the hand-raise path (which
        # only queues, never speaks) -> re-raise to preserve today's behavior; a
        # pre-token failure must not be swallowed into silence when llm.py's
        # fallback (or Recall's re-delivery) is the existing recovery. No
        # transcript is logged — PII: only the exception class name.
        if hand_mode or not speak_tasks:
            raise
        print(
            f"[live] answer stream dropped after first token "
            f"({type(e).__name__}); speaking one recovery line",
            flush=True,
        )
        if session.speech_generation == turn_gen:
            recovery = _line_for(
                question or text, _STREAM_RECOVERY_LINES, _STREAM_RECOVERY_LINES_IT
            )
            speak_tasks.append(
                asyncio.create_task(
                    _speak_with_audio(
                        session,
                        recovery,
                        force=True,
                        generation=turn_gen,
                        prev=prev_task,  # chain after the in-flight sentence(s)
                    )
                )
            )
        results = await asyncio.gather(*speak_tasks, return_exceptions=True)
        spoke_any = any(r is True for r in results)
        return JSONResponse(
            {"ok": True, "spoke": spoke_any, "streamed": True, "recovered": True}
        )

    if hand_mode:
        if interrupted or session.speech_generation != turn_gen:
            return JSONResponse({"ok": True, "spoke": False, "interrupted": True})
        if hand_sentences:
            # Cap the queued point at spoken length: a raised hand buys a
            # remark, not a lecture.
            contribution = " ".join(hand_sentences)[:600]
            # Motivation gate: grounded (SKIP already passed) is necessary but
            # not sufficient — the same point must not raise the hand twice,
            # and raising has a social budget (cap + pacing + back-off after
            # being ignored). A suppressed point isn't lost to the meeting:
            # the finalize summarizer still reads the whole transcript.
            if similar_contribution(contribution, session.hand_last_contribution):
                return JSONResponse(
                    {"ok": True, "spoke": False, "reason": "hand suppressed (same point)"}
                )
            # ── ONE shared social budget for an interjection AND a raised hand ──
            # Grounded (SKIP passed) + not-a-duplicate is necessary but not
            # sufficient: taking the floor at all — spoken interjection OR silent
            # hand — draws from the SAME per-meeting budget (cap + minimum gap +
            # longer back-off after the room ignored a raise). Checked ONCE, up
            # front, so a "single-interjection" stays singular: without this the
            # spoken escape would bypass the budget and she could interject every
            # cooldown. Budget spent → stay silent (the finalize summarizer still
            # reads the whole transcript, so the point is never lost).
            if not should_raise_hand(
                now=time.time(),
                count=session.hand_raise_count,
                last_at=session.hand_last_raise_at,
                last_ignored=session.hand_last_ignored,
                max_per_meeting=settings.hand_raise_max_per_meeting,
                min_gap_seconds=settings.hand_raise_min_gap_seconds,
                ignored_gap_seconds=settings.hand_raise_ignored_gap_seconds,
            ):
                return JSONResponse(
                    {"ok": True, "spoke": False, "reason": "hand suppressed (budget)"}
                )
            # ── high-confidence interjection escape ──
            # Grounded, non-duplicate, and within budget. If it is ALSO strongly
            # grounded (top retrieval score ≥ the bar) AND the floor is open (the
            # line that opened it sounds FINISHED and no human is audibly
            # mid-utterance), say ONE line directly instead of raising a silent
            # hand nobody may notice+invite in time — the marquee "she jumped in
            # with the right fact" beat. Weaker or floor-busy points fall through
            # to the raised hand. The generation was already bumped for this turn;
            # barge-in during generation is caught by the interrupted check above,
            # so speaking under turn_gen here is safe.
            _top_score = float(_answer_meta.get("top_score", 0.0))
            # Cross-talk: a tight two-human back-and-forth closes the floor for an
            # UNPROMPTED interjection — she falls to the audio-silent raised hand
            # (waiting to be invited) instead of talking over their volley.
            _dyad = settings.cross_talk_suppression_enabled and in_locked_dyad(
                session.human_transcript(),
                avatar_name=avatar.name,
                now=time.time(),
                min_turns=settings.cross_talk_min_turns,
                max_gap_seconds=settings.cross_talk_max_gap_seconds,
                window=settings.cross_talk_window,
            )
            if should_interject(
                enabled=settings.hand_raise_interject_when_confident,
                confidence=_top_score,
                min_confidence=settings.hand_raise_interject_min_confidence,
                floor_open=(not _dyad) and interjection_floor_open(
                    turn_completeness=end_of_turn.completeness(text),
                    since_human_partial=time.time() - session.last_human_partial_at,
                    active_partial_seconds=settings.interject_min_pause_seconds,
                    min_completeness=settings.interject_min_completeness,
                    # Re-check the floor at SPEAK time: a human may have taken it
                    # while she generated. Off → today's single trigger-time read.
                    transcript_grew=(
                        settings.interject_recheck_floor_at_speak
                        and len(session.human_transcript()) > _interject_len0
                    ),
                    generation_elapsed=(
                        time.time() - _interject_t0
                        if settings.interject_recheck_floor_at_speak
                        else None
                    ),
                ),
            ):
                # Charge the interjection to the shared budget EXACTLY as
                # _raise_hand does: it counts against the cap, paces the next one
                # (hand_last_raise_at), and — since it was ENGAGED (spoken), not
                # ignored — resets the back-off. Also dedup a future repeat of
                # this same point (hand OR interject).
                session.hand_raise_count += 1
                session.hand_last_raise_at = time.time()
                session.hand_last_ignored = False
                session.hand_last_contribution = contribution
                spoke = await _speak_with_audio(
                    session, contribution, force=True, generation=turn_gen, prev=None
                )
                return JSONResponse(
                    {
                        "ok": True,
                        "spoke": bool(spoke),
                        "interjected": True,
                        "confidence": round(_top_score, 3),
                    }
                )
            session.pending_contribution = contribution
            await _raise_hand(session, avatar, heard=text)
            return JSONResponse({"ok": True, "spoke": False, "hand_raised": True})
        return JSONResponse(
            {"ok": True, "spoke": False, "reason": "insufficient context (SKIP)"}
        )

    if not interrupted and speak_tasks:
        results = await asyncio.gather(*speak_tasks, return_exceptions=True)
        spoke_any = any(r is True for r in results)
        suppressed_any = any(r is not True for r in results)

    if interrupted or session.speech_generation != turn_gen:
        # The turn died mid-answer — report it honestly and skip the repair
        # line (a human is talking; silence is correct). Any still-pending
        # speak tasks drop themselves via the generation check.
        return JSONResponse({"ok": True, "spoke": spoke_any, "interrupted": True})

    if not spoke_any:
        if suppressed_any:
            # She had an answer but already said exactly this recently —
            # stay silent and say so, instead of claiming she spoke.
            return JSONResponse(
                {"ok": True, "spoke": False, "reason": "duplicate answer suppressed"}
            )
        if _should_repair_silent_answer(called, text):
            line = _silent_answer_repair_line(avatar)
            if await _make_avatar_speak(session, line):
                return JSONResponse(
                    {
                        "ok": True,
                        "spoke": True,
                        "reason": "repair_after_skip",
                        "line": line,
                    }
                )
            return JSONResponse(
                {"ok": True, "spoke": False, "reason": "repair suppressed (said recently)"}
            )
        return JSONResponse(
            {"ok": True, "spoke": False, "reason": "insufficient context (SKIP)"}
        )
    return JSONResponse({"ok": True, "spoke": True, "streamed": True})
