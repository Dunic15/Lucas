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
    avatar_resolver,
    avatars,
    browser_meeting,
    store,
    recall_client,
    anam_client,
    action_plane,
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
    graphiti_client,
    granola_client,
    jira_client,
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
from .brain import capabilities  # deterministic org-scoped capability truth
from .brain.engine import (
    SEARCH_ANNOUNCE_LINES,
    answer_question,
    answer_question_stream,
    answer_with_tools,
    is_tool_domain_ask,
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
from .meeting import conversation_frame
from .meeting.lifecycle import (  # noqa: E402  (hoisted lifecycle core; re-import = compat)
    _BOT_TERMINAL, _BOT_VARIANT_RANK, _LEAVE_GONE_STATUSES,
    _ACTION_STOP, _DEMO_BROWSE_RE, _finalizing, _graphiti_tasks, _recall_list_headers,
    _bot_meeting_key, _bot_variant_rank, _bot_status_code, _status_change_epoch,
    _avatar_asana_enabled, _stamp_action_routing, _norm_action_text, _content_tokens,
    _same_action, _is_live_browse_item, _merge_action_items, _fold_into_live,
    _close_usage_for, _assign_usage_bot_id, _abandon_orphan_session,
    _leave_confirmed_stopped, _bot_reports_terminal, _retry_leave,
    _start_avatar_session, _finalize_session, _finalize_session_locked,
)
from .api.deps import EMAIL_RE, _split_emails, _calendar_target_emails, _gmail_state, _line_for  # noqa: E402
from .config import settings
from .decision import (
    addressed_to_other,
    adaptive_deference_seconds,
    closing_fallback_fires,
    detect_wake,
    detect_closing,
    detect_invite,
    detect_browse_intent,
    detect_browse_dismiss,
    browse_signal,
    detect_leave_command,
    detect_leave_command_explicit,
    detect_stop_command,
    fuzzy_name_match,
    in_locked_dyad,
    interjection_floor_open,
    is_capture_continuation,
    plausible_leave_followup,
    should_interject,
    should_raise_hand,
    similar_contribution,
)
from .rag import ensure_about_index, ensure_index, warm as warm_index

def _browser_expire_tick() -> None:
    """Close+expire browser sessions past TTL for every org that has due ones
    (the server-side meter-safety guard, independent of Laura's own close).
    Sync; called via run_in_threadpool from the worker loop. Never raises."""
    try:
        from .browser import dal as browser_dal
        from .browser import provider as browser_provider

        for org_id in browser_dal.orgs_with_due_sessions():
            for row in browser_dal.expire_due(org_id):
                try:
                    browser_provider.get_provider(
                        row["provider"]).close(row["provider_ref"])
                except Exception:  # noqa: BLE001 — provider release best-effort
                    pass
    except Exception as exc:  # noqa: BLE001 — never break the worker loop
        print(f"[browser] expire tick failed: {type(exc).__name__}",
              flush=True)


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

    # Index warm-up runs in the BACKGROUND. On a fresh instance (ephemeral
    # disk) it downloads the local embedding model — measured 3-5 minutes on
    # 2026-07-22 — and running it synchronously here kept /health from
    # binding until it finished, flipping deploys into health-check-rollback
    # roulette (~50%: two ROLLBACK_SUCCEEDED out of four boots that day).
    # The 2026-07-16 fail-soft covered HF being DOWN; this covers HF being
    # SLOW. Deploys happen in no-meeting windows (sessions gate), so the
    # warm-up finishes long before the first live question; a question racing
    # it pays the old cold-start once — never a failed deploy.
    async def _warm_indexes() -> None:
        _t0 = time.perf_counter()
        # While this runs, the LIVE answer path skips retrieval entirely
        # (rag.is_warming) — 2026-07-24 the warm-up took 1480s after a docs
        # change and a meeting inside that window went MUTE: every retrieve
        # queued behind the rebuild (106-121s), every answer was cancelled.
        from .brain import rag as _rag

        _rag.set_warming(True)
        try:
            await run_in_threadpool(_prebuild_indexes)
            print(
                f"[startup] index warm-up complete in "
                f"{time.perf_counter() - _t0:.0f}s",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 — warm-up must never kill boot
            print(
                f"[startup] index warm-up failed: {type(exc).__name__}",
                flush=True,
            )
        finally:
            _rag.set_warming(False)

    asyncio.create_task(_warm_indexes())

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
                    # Data Foundation sync runs ride the same tick when the
                    # flag is on (claim/lease — safe on every instance).
                    from . import datafoundation

                    if datafoundation.enabled():
                        from .datafoundation import sync as df_sync

                        await run_in_threadpool(df_sync.process_due)
                    # Browser B0: expire sessions past TTL server-side even if
                    # nothing else closes them (the third meter-safety guard).
                    from . import browser

                    if browser.enabled():
                        await run_in_threadpool(_browser_expire_tick)
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
from . import org_avatars_api  # noqa: E402

app.include_router(org_avatars_api.router)  # /org/avatars + Avatar Studio twin (M2)
from .datafoundation import router as df_router  # noqa: E402

app.include_router(df_router.router)  # /org/data + dashboard twin (DF0-DF1)
from .browser import router as browser_router  # noqa: E402

app.include_router(browser_router.router)  # /org/browser + dashboard twin (B0)
from .demo_mvp import router as demo_router  # noqa: E402

app.include_router(demo_router.router)  # /org/demo + dashboard twin (Northstar MVP)
from . import pipedream_api  # noqa: E402

app.include_router(pipedream_api.router)
from .api import pages  # noqa: E402
app.include_router(pages.router)  # static pages + avatar assets
from .api import granola  # noqa: E402
app.include_router(granola.router)  # /granola/*
from .api import oauth  # noqa: E402
app.include_router(oauth.router)  # /oauth/{google,asana,jira}/*
from .api import avatars_api  # noqa: E402
app.include_router(avatars_api.router)  # /avatars*
from .api import meetings  # noqa: E402
app.include_router(meetings.router)  # /ledger, /meetings*
from .api import health as health_api  # noqa: E402
app.include_router(health_api.router)  # /health*, /recall/status, /gmail/status, ears
from .api import console  # noqa: E402
app.include_router(console.router)  # /, /demo/*, /live/*
from .api import sessions  # noqa: E402
app.include_router(sessions.router)  # /sessions/*  # /dashboard/pipedream (alt connections, flag-gated)

# Meeting-bound GPU runtime re-checks the live session count before it stops
# the photoreal box (a new meeting may have started during the grace window).
gpu_runtime.configure(lambda: len(store.all_sessions()))
runpod_runtime.configure(lambda: len(store.all_sessions()))

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
REPO_ROOT_DIR = Path(__file__).resolve().parents[2]
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
            posted = False
            if text and settings.slack_webhook_url:
                # Dedupe: the boot-time first run re-posted the SAME warning
                # on every deploy (vendor_health.should_post).
                if await run_in_threadpool(vendor_health.should_post, text):
                    await run_in_threadpool(actions.post_to_slack, text)
                    await run_in_threadpool(vendor_health.mark_posted, text)
                    posted = True
            bad = [r for r in results if r["status"] in ("warn", "crit")]
            print(
                f"[vendors] check: {len(results) - len(bad)} ok, {len(bad)} "
                f"da attenzionare"
                f"{' (postato su Slack)' if posted else ''}"
                f"{' (ripetizione soppressa)' if text and settings.slack_webhook_url and not posted else ''}",
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


# ── Google Calendar OAuth: connect Laura's calendar to Recall Calendar V2 ──
# Per-browser CSRF state for THIS flow. The nonce lives in an HttpOnly cookie
# scoped to /oauth (covers both connect and callback) and, signed, inside the
# `state` param — the callback requires both to match. CALENDAR_STATE_PURPOSE
# domain-separates the signature from the login flow's "state", so neither
# flow's state can be replayed in the other. This REPLACES the old static
# settings.calendar_oauth_state as the real CSRF barrier.












# ── Asana OAuth: the dashboard's one-click "Connect Asana" (docs/ASANA.md) ──
# Same CSRF machinery as the Google flow above: single-use signed state in the
# provider redirect, nonce in an HttpOnly cookie, both must match at the
# callback. Requires the Asana OAuth app env (ASANA_CLIENT_ID/SECRET); without
# it the Connections card falls back to the paste-a-PAT flow.








# ── Jira (Atlassian 3LO) OAuth: "Connect Jira" → login → connected ──
# Mirrors the Asana flow. Requires the Atlassian OAuth app env (JIRA_CLIENT_ID/
# SECRET); without it the Connections card falls back to the paste-a-token flow.








# ── Granola: pull a real finished transcript (post-meeting only) ──




# ──────────────────────── session lifecycle ────────────────────────
















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






# The post-meeting summarizer rephrases a live how-to/tour ask into a
# THIRD-person to-do — "Show Duccio how to create a task", "Give Duccio a tour
# of the Asana dashboard", "Show Duccio step-by-step what to click". Those miss
# detect_browse_intent (first-person-anchored: "show me how to…"), so without
# this they land in the to-dos AND get executed as real Asana tasks. Catch the
# demonstrative rephrasing too: a teaching/showing verb near a how-to/tour
# marker. These are performed LIVE on the avatar's tile, never a to-do.

# Per-meeting walkthrough epoch (keyed by bot_id), DECOUPLED from
# speech_generation. A running browser walkthrough is cancelled only when this
# advances — on a new browse ask or an explicit dismiss — never by the ambient
# per-utterance generation churn of a 1:1 meeting, which was draining a turn
# backlog the instant we awaited the walkthrough and cancelling every tour at
# step 0. Bounded by live meetings; overwritten per ask.
_BROWSE_EPOCH: dict[str, int] = {}

# Per-meeting in-flight debounce (bot_id -> monotonic deadline). While a browse
# is opening (provider cold-start + saved-login load is ~15-20s), re-asks from
# the human — who can't see anything happening yet — are IGNORED instead of
# spawning a second open and advancing the epoch, which cancelled the first
# walkthrough. Auto-expires (backstop) and is cleared the moment the view is on
# the tile, so a genuine follow-up ("...now create a task") is still honored.
_BROWSE_INFLIGHT: dict[str, float] = {}












# CEDRIC: live context push — the orchestrator POSTs a fresh brief the moment
# something changes (real-time counterpart of the periodic context pull);
# inject_brief re-reads per turn, so the next answer speaks from it.






















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


# On-demand snapshot pull (owner 2026-07-24): "get/pull/refresh the snapshot",
# "check Asana and get a snapshot", "pull my inbox". Deliberately explicit
# verbs — a mere MENTION of "snapshot" in conversation never triggers a fetch,
# and "do you HAVE a snapshot?" (no pull verb) stays with the deterministic
# capability answer.
_SNAPSHOT_PULL = re.compile(
    r"\b(?:get|pull|grab|load|fetch|refresh|update|scarica|aggiorna|prendi)\b"
    r".{0,28}\bsnapshot\b"
    r"|\bsnapshot\b.{0,20}\b(?:now|adesso|ora)\b"
    r"|\bcheck\s+(?:my\s+)?asana\b.{0,30}\b(?:get|snapshot)\b"
    r"|\b(?:pull|refresh|reload|ricarica)\b.{0,16}\b(?:my|our|mio|mia)\s+"
    r"(?:asana|inbox|gmail|board)\b",
    re.IGNORECASE,
)

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
    "Got it — it'll be on the dashboard for your approval right after the call.",
    "Noted — I'll line it up on the dashboard for approval once we wrap.",
    "On it — it goes to the dashboard for your approval right after this meeting.",
]
_QUEUE_LINES_IT = [
    "Ricevuto — lo trovi in dashboard per l'approvazione appena finiamo.",
    "Segnato — va in dashboard per l'approvazione subito dopo la call.",
]

# Task-creation captures get the fuller pointer: the fields that are better
# filled by click than by voice (subtasks / dependencies / attachments) live on
# the approval card. Fixed lines -> TTS-prewarmed, instant.
_QUEUE_LINES_TASK = [
    "Got it — it'll be on the dashboard for your approval. Subtasks, dependencies or attachments: add them right on the card.",
    "Noted — approve it on the dashboard after the call; you can attach files or add subtasks and dependencies there too.",
]
_QUEUE_LINES_TASK_IT = [
    "Ricevuto — lo approvi in dashboard a fine call; sottoattività, dipendenze o allegati li aggiungi direttamente sulla scheda.",
]


def _queue_line_for(heard: str, item: dict | None) -> str:
    """Confirmation for a capture: only genuine TASK creations get the card
    pointer (subtasks/dependencies/attachments); a calendar meeting or email
    gets the classic line. Uses ask_kind, not a bare "create" match — live
    2026-07-23 "create a meeting" wrongly got the task line ("subtasks,
    dependencies or attachments"), which is meaningless for a calendar event."""
    if tools.ask_kind(str((item or {}).get("action") or "")) == "task":
        return _line_for(heard, _QUEUE_LINES_TASK, _QUEUE_LINES_TASK_IT)
    return _line_for(heard, _QUEUE_LINES, _QUEUE_LINES_IT)

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
# ONE vocabulary across every surface (Phase 1, owner 2026-07-22): the voice
# clarify speaks the same canonical labels the dashboard chips/forms render —
# action_plane owns them all (param ↔ slot ↔ label).
_CLARIFY_SLOTS = action_plane.SLOT_LABELS_EN
_CLARIFY_SLOTS_IT = action_plane.SLOT_LABELS_IT
_CLARIFY_WINDOW_S = 45.0  # after this, resolve quietly with what we have
_SAME_ASK_WINDOW_S = 90.0  # a retry of an ask captured this recently merges
_ALREADY_LINES = [
    "Already on the list — I've noted that.",
    "Got it — that one's already queued.",
]
_ALREADY_LINES_IT = [
    "È già in lista — annotato.",
    "Ricevuto — quella è già in coda.",
]

# Spoken when the asker cancels the action she just captured ("lascia
# perdere", "never mind"): the draft card is withdrawn for real (spec M
# 2026-07-22 — before this, the cancel phrase was APPENDED to the action text
# and the dead card still reached the dashboard). Fixed lines ⇒ prewarmed TTS.
_CANCEL_ACK_LINES = [
    "Okay — scrapped it.",
    "Done, I've dropped that one.",
]
_CANCEL_ACK_LINES_IT = [
    "Ok, lascio perdere — annullata.",
    "Va bene, la scarto.",
]


def _clarify_line(heard: str, missing: list[str]) -> str:
    # Always LEAD with a capture confirmation, THEN ask for the missing detail —
    # the action is already durably captured at this point, so the owner must
    # hear it was taken even when a detail is still needed (live repro 2026-07-21:
    # Petra asked for details but never confirmed she'd queued anything).
    if sounds_italian(heard):
        slots = [_CLARIFY_SLOTS_IT[m] for m in missing if m in _CLARIFY_SLOTS_IT]
        joined = slots[0] if len(slots) == 1 else ", ".join(slots[:-1]) + " e " + slots[-1]
        return f"Fatto, la metto in coda per l'approvazione. Prima però: {joined}?"
    slots = [_CLARIFY_SLOTS[m] for m in missing if m in _CLARIFY_SLOTS]
    joined = slots[0] if len(slots) == 1 else ", ".join(slots[:-1]) + ", and " + slots[-1]
    return f"Got it — I'll queue that for your approval. First though: {joined}?"


def _bind_pending_action(
    session: store.Session,
    item: dict,
    speaker_id: str,
    created_at: float,
    missing: list[str],
    source_event_key: str,
    source_fingerprint: str,
    heard: str,
) -> str:
    """Mirror the legacy live tuple into one typed canonical pending record."""
    line = _clarify_line(heard, missing)
    store.bind_pending_action(
        session,
        item,
        speaker_id,
        tools.ask_kind(item.get("action") or ""),
        missing,
        collected_parameters=tools.collected_action_parameters(
            item.get("action") or ""
        ),
        question=line,
        source_event_key=source_event_key,
        source_fingerprint=source_fingerprint,
    )
    session.pending_clarify = (
        item, speaker_id, created_at, missing,
        source_event_key, source_fingerprint,
    )
    return line


def _settle_pending_action(
    session: store.Session, item: dict, speaker_id: str, status: str
) -> None:
    store.settle_pending_action(session, item, speaker_id, status)
    session.pending_clarify = None


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

# A slow lookup is a small asynchronous job with an explicit ready state.
# These follow-ups must consume its result, never launch an unrelated
# Asana/docs answer for the same question.
_SEARCH_RESULT_REQUEST = re.compile(
    r"\b(?:did\s+you\s+(?:find|search|check|look)|have\s+you\s+found|"
    r"what\s+did\s+you\s+find|any\s+results?|do\s+you\s+have\s+(?:it|the\s+result)|"
    r"hai\s+trovato|cosa\s+hai\s+trovato|ci\s+sono\s+risultati|risultato)\b",
    re.IGNORECASE,
)
_SEARCH_STILL_LINES = [
    "I'm still checking — I'll raise my hand as soon as the result is ready.",
    "Still searching. I'll signal the room the moment I have it.",
]
_SEARCH_STILL_LINES_IT = [
    "Sto ancora cercando — alzo la mano appena ho il risultato.",
    "Controllo ancora. Vi segnalo appena è pronto.",
]


def _is_search_result_request(text: str) -> bool:
    return bool(_SEARCH_RESULT_REQUEST.search(text or ""))


def _avatar_voice(session: "store.Session") -> str:
    """The session avatar's ElevenLabs voice for TTS. avatars.load is
    mtime-cached (hot-path safe); any failure falls back to the global voice
    ("" keeps the shared prewarm cache key)."""
    try:
        return avatar_resolver.for_session(session).elevenlabs_voice_id or ""
    except Exception:  # noqa: BLE001
        return ""




def _human_count(session: "store.Session", avatar: avatars.Avatar) -> int:
    """Humans in the room, EXCLUDING the avatar's own Recall bot.

    The bot joins Meet/Zoom under ``avatar.name`` and Recall's participant-join
    payload carries no ``is_bot`` flag, so ``resolve_participant`` classifies it
    as a human (names are deliberately never identity — store.py). ``roster()``
    then counts the bot as a second person, so a genuine 1:1 (one human + the
    bot) reads as 2 and every "solo" relaxation below stays off — the avatar
    only answered when named (the fluidity bug 2026-07-21; PR #341 added the
    relaxations but never fixed the count). Subtract at most ONE bot-named entry:
    a real 2-human call still reads as ≥2, so multiparty deference is untouched;
    the only edge (a real human literally named like the avatar) merely makes the
    room slightly more fluid, still gated by deference + cooldown + SKIP."""
    roster = session.roster(avatar.name)
    bot = (avatar.name or "").strip().lower()
    n = len(roster)
    if bot and any((r or "").strip().lower() == bot for r in roster):
        n -= 1
    return max(n, 0)


def _wake_required(avatar: avatars.Avatar,
                   session: "store.Session | None" = None) -> bool:
    """Whether THIS avatar speaks only when addressed by name — the per-avatar
    require_wake_word (avatar.yaml), inheriting the global REQUIRE_WAKE_WORD
    when unset. When true, every unprompted speech path is silenced: answers,
    backchannels, joiner greetings, quiet nudges, confident interjections, and
    the proactive closing intervention. What still speaks: being called by
    name and follow-ups right after the avatar's own answer (a reply to her
    is not an interruption). Even the one-time self-introduction on join is
    suppressed: a wake-word avatar enters SILENT and only listens/transcribes
    until it's addressed by name (owner ask 2026-07-20)."""
    base = (
        avatar.require_wake_word
        if avatar.require_wake_word is not None
        else settings.require_wake_word
    )
    if not base:
        return False
    # 1:1 relaxation (owner 2026-07-20): with a single human present, the ask
    # is unambiguously for her, so no wake word is needed — the conversation
    # stays fluid. Groups keep name-required (unprompted speech there risks
    # interrupting). Deference + cooldown still gate every reply.
    if session is not None and _human_count(session, avatar) <= 1:
        return False
    return base


def _followup_shape_ok(text: str) -> bool:
    """Does a bare (un-named) line within the follow-up window SOUND like a
    follow-up to her answer? Questions always did ("e la deadline?"); direct
    imperative asks now do too ("crea una task", "send him the doc") — live
    repro 2026-07-22: the hard endswith('?') gate made every un-punctuated
    imperative follow-up require re-saying her name, breaking the flow the
    window exists for. wants_action_capture is the same ^-anchored regex the
    capture seam trusts, so plain statements ("we should send X") stay out,
    and the in-stream SKIP sentinel remains the backstop for misfires."""
    t = (text or "").rstrip()
    return t.endswith("?") or wants_action_capture(t)


def _followup_speaker_ok(session: "store.Session", speaker_id: str) -> bool:
    """Bind the follow-up window to the participant whose ask she just served
    (multi-party etiquette): THEIR un-named follow-up rides the fast path;
    a different participant addresses her by name. Turns with no recorded
    interlocutor (proactive/closing speech) keep the historical any-speaker
    behavior, as do sessions where the speaker id is unknown."""
    owner = getattr(session, "followup_owner", None)
    if not owner or not speaker_id:
        return True
    return bool(owner[0] == speaker_id)


# Empty-room backstop (live repro 2026-07-22: the humans finished and hung
# up expecting her to follow; nothing watched the roster, so the bot — and
# the per-minute meter — sat in the dead room until a manual End). When the
# LAST human leaves, wait a grace period (someone may rejoin after a drop),
# re-check, then run the same idempotent finalize as POST /sessions/end.
# Tunable via EMPTY_ROOM_GRACE_SECONDS (settings.empty_room_grace_seconds);
# tests monkeypatch this module attribute directly.
_EMPTY_ROOM_GRACE_S = settings.empty_room_grace_seconds


def _invalidate_empty_room_leave(session: "store.Session") -> None:
    """Invalidate the current empty-room deadline when the room is occupied.

    A join starts a new room-presence generation. Cancelling and clearing the
    task lets a later non-empty -> empty transition receive a full grace period
    instead of inheriting the deadline from an earlier disconnect.
    """
    session.empty_room_generation = (
        getattr(session, "empty_room_generation", 0) + 1
    )
    task = getattr(session, "empty_room_task", None)
    if task is not None and not task.done():
        task.cancel()
    session.empty_room_task = None


def _schedule_empty_room_leave(session: "store.Session") -> None:
    task = getattr(session, "empty_room_task", None)
    if task is not None and not task.done():
        return  # already timing this exact empty-room generation

    generation = getattr(session, "empty_room_generation", 0) + 1
    session.empty_room_generation = generation

    async def _check(bot_id: str, expected_generation: int) -> None:
        try:
            await asyncio.sleep(_EMPTY_ROOM_GRACE_S)
            live = store.get(bot_id)
            if live is None:
                return  # already finalized elsewhere
            if (
                getattr(live, "empty_room_generation", 0)
                != expected_generation
            ):
                return  # a join invalidated this deadline
            av = avatar_resolver.for_session(live)
            if live.roster(av.name):
                return  # someone rejoined during the grace window
            print(
                f"[leave] room empty for {_EMPTY_ROOM_GRACE_S:.0f}s — "
                "auto-finalizing (meter safety)",
                flush=True,
            )
            await _finalize_session(bot_id, source="empty_room")
        except asyncio.CancelledError:
            return  # a participant rejoined; the next leave gets a fresh timer
        except Exception as exc:  # noqa: BLE001 — a failed check must not crash the loop
            print(f"[leave] empty-room check failed ({type(exc).__name__})", flush=True)
        finally:
            live = store.get(bot_id)
            if (
                live is not None
                and getattr(live, "empty_room_generation", 0)
                == expected_generation
                and getattr(live, "empty_room_task", None)
                is asyncio.current_task()
            ):
                live.empty_room_task = None

    session.empty_room_task = asyncio.create_task(
        _check(session.bot_id, generation)
    )


_LEADING_VOCATIVE = re.compile(r"^\s*([A-Za-zà-ù]+)\s*,\s+")
_VOCATIVE_FILLERS = {
    "ok", "okay", "yes", "yeah", "no", "hey", "hi", "hello", "ciao", "si",
    "sì", "allora", "senti", "scusa", "grazie", "thanks", "so", "well",
    "right", "sure", "perfetto", "bene",
}


def _addressed_elsewhere(
    session: "store.Session", avatar: avatars.Avatar, text: str
) -> bool:
    """The line opens a NEW turn aimed at someone else — never glue it onto a
    captured action or read it as the answer to her clarify question (live
    repro 2026-07-22: '…please? Ducho, do you wanna discuss something else?'
    was appended to the card, and 'Ducho' was mangled enough that the roster
    fuzzy match alone missed it). Two nets: the roster vocative check, plus a
    leading 'Name, …' whose name is NOT the avatar's — an ASR split of one
    ask resumes mid-phrase, essentially never with a fresh vocative."""
    if addressed_to_other(text, session.roster(avatar.name)):
        return True
    m = _LEADING_VOCATIVE.match(text or "")
    if not m:
        return False
    tok = m.group(1).lower()
    if tok in _VOCATIVE_FILLERS:
        return False
    return not any(fuzzy_name_match(tok, w) for w in avatar.wake_words)


def _active_draft(session: "store.Session") -> tuple[dict | None, str]:
    """The action draft the room can still amend by voice, plus its asker:
    a capture parked in clarify, else the most recent capture inside the
    same-ask horizon. (None, "") when nothing is amendable."""
    clar = getattr(session, "pending_clarify", None)
    if clar is not None:
        return clar[0], str(clar[1] or "")
    recent = getattr(session, "last_capture", None)
    if recent is not None and (time.time() - recent[2]) < _SAME_ASK_WINDOW_S:
        return recent[0], str(recent[1] or "")
    return None, ""


def _should_backchannel(
    session: store.Session, text: str, avatar: "avatars.Avatar | None" = None
) -> bool:
    """A human is deep into a long utterance and she's been silent a while —
    one tiny cue ("Mm-hm.") reads as listening. Deliberately rare: long
    partials only, one per gap window, never while (or right after) she talks,
    so it stays a nod and never becomes chatter."""
    if not settings.backchannel_enabled:
        return False
    if avatar is not None and _wake_required(avatar, session):
        return False  # wake-word mode: never make an unprompted sound
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
        *_QUEUE_LINES_TASK,
        *_QUEUE_LINES_TASK_IT,
        *_CANCEL_ACK_LINES,
        *_CANCEL_ACK_LINES_IT,
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
        if avatar_resolver.for_session(session).silent:
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
    try:
        _frame_avatar_name = avatar_resolver.for_session(session).name
    except Exception:  # noqa: BLE001 — shadow state must never block speech
        _frame_avatar_name = (session.avatar_id or "avatar").title()
    conversation_frame.observe_session_avatar_speech_started(
        session,
        avatar_name=_frame_avatar_name,
        generation=message["generation_id"],
    )
    # Estimate how long this line keeps her talking; queued lines extend it.
    # With server-synthesized audio the REAL duration is known from the last
    # word's timings — barge-in stops estimating and starts knowing.
    est = max(1.0, len(text.split()) / _SPEECH_WORDS_PER_SECOND)
    if audio and audio.get("wtimes") and audio.get("wdurations"):
        est = max(1.0, (audio["wtimes"][-1] + audio["wdurations"][-1]) / 1000 + 0.3)
    session.speaking_until = max(session.speaking_until, time.time()) + est
    # Archive her side of the conversation verbatim at dispatch — Recall never
    # transcribes the bot's own output audio, so without this the meeting
    # transcript shows only the humans. kind="agent" keeps the line out of the
    # roster, MeetingState, and every evidence/analysis path (those all filter
    # on speaker_kind); the synthetic participant_id skips the name-match
    # rebind in add_utterance. Backchannels ("Mm-hm.") are listening cues, not
    # turns — they stay out of the archive like they stay out of the cooldown.
    if not backchannel:
        try:
            _speaker_name = avatar_resolver.for_session(session).name
        except Exception:  # noqa: BLE001 — a config read must never mute her
            _speaker_name = (session.avatar_id or "avatar").title()
        session.add_utterance(
            _speaker_name, text, participant_id="agent:self",
            speaker_kind="agent",
        )
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
    """Put the hand down and clear the queued contribution metadata."""
    session.hand_raised_at = 0.0
    session.pending_contribution = ""
    session.pending_contribution_kind = ""
    session.pending_contribution_query = ""
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
    # Solo-fluid relaxation (owner ask 2026-07-21, round-3 live repro): with ONE
    # human present the meeting IS a conversation with her — there is no room to
    # settle into and nobody she could talk over. Without this, the
    # first_call_required default turns a 1:1 where nobody says her name into a
    # PERMANENT silent guest (Petra answered nothing for a whole solo meeting),
    # making the #306 fluid mode unreachable in exactly the case it exists for.
    try:
        _av = avatar_resolver.for_session(session)
        if _human_count(session, _av) <= 1:
            return False
    except Exception:  # noqa: BLE001 — resolution trouble: keep the strict gate
        pass
    if settings.first_call_required:
        return True
    if settings.opening_grace_seconds <= 0:
        return False
    return (time.time() - session.created_at) < settings.opening_grace_seconds


# Strong refs to in-flight self-intro tasks: a bare create_task is only weakly
# held by the loop and can be GC'd mid-sleep, silently killing the feature (same
# pattern as _summary_tasks above).
_self_intro_tasks: set = set()

# Strong refs to in-flight graphiti ingest tasks (fired off the join path).
# KEEP — merges keep reverting this.

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
    # Wake-word avatars enter SILENT — no self-introduction on join (owner ask
    # 2026-07-20): they just listen and transcribe until someone says their
    # name. Mark it done so we stop re-checking on every webhook.
    try:
        if _wake_required(avatars.load(session.avatar_id)):
            session.self_introduced = True
            return False
    except Exception:  # noqa: BLE001 — resolution must never break the join path
        pass
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
        avatar = avatar_resolver.for_session(session)
    except Exception:  # noqa: BLE001 — never let a config read crash a bg task
        return
    # Language follows the room if anything was heard, else defaults to English.
    heard = session.recent_transcript(3) if session.transcript else ""
    # An org overlay may provide the greeting verbatim (M2). Spoken as-is —
    # bounded + sanitized at overlay-write time; like every self-intro line it
    # is dynamic and therefore never TTS-prewarmed (one lazy synth, off-path).
    greeting = ""
    if getattr(avatar, "overlay_version", 0):
        greeting = str(
            getattr(avatar, "resolver_provenance", {}).get("greeting") or ""
        )
    line = greeting or _line_for(
        heard, _SELF_INTRO_LINES, _SELF_INTRO_LINES_IT
    ).format(name=avatar.name)
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
    ears_off = settings.gemini_ears_mode.strip().lower() == "off"
    # Recall-only mode: ASR word-drift breaks the substring test the same way
    # ("Who owns it…" spoken -> "who owned it…" transcribed, round-4 live repro
    # 2026-07-21 — her clarify question re-entered as a 'human' line). Scope the
    # fuzzy test to the seconds right after she actually spoke (echo is only
    # physically possible then); outside that window keep the strict behavior
    # so a human paraphrasing her minutes later is never eaten.
    if ears_off and time.time() - getattr(session, "last_spoke_at", 0.0) > 12.0:
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
            # M2 overlay stash (behavioral personalization for the live path).
            # The page URL above was built BEFORE org attribution, so autojoin
            # keeps the canonical face/body — a documented limitation; name,
            # persona, greeting, voice and tool narrowing still apply.
            try:
                resolved = await run_in_threadpool(
                    avatar_resolver.resolve_for_dispatch,
                    dispatch_org, avatar.id,
                )
                if getattr(resolved, "overlay_version", 0):
                    s.resolved_avatar = resolved
            except Exception:  # noqa: BLE001 — overlays must never break autojoin
                pass
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


_FINAL_DEDUPE_WINDOW_S = 8.0
_FINAL_DEDUPE_MAX = 256


def _is_duplicate_recall_final(
    session: store.Session,
    event_key: str,
    fingerprint: str,
    *,
    now: float | None = None,
) -> bool:
    """Sliding, in-memory dedupe for repeated Recall final transcriptions.

    Keys are SHA-256 digests only: no transcript text is stored or logged. The
    fingerprint window refreshes on every consecutive duplicate, so a provider
    replay loop stays suppressed until it stops. An intervening distinct final
    is meaningful conversation and resets phrase dedupe; exact event identities
    remain suppressed across the whole bounded window.
    """

    stamp = time.monotonic() if now is None else float(now)
    seen = getattr(session, "_recall_final_dedupe", None)
    if not isinstance(seen, dict):
        seen = {}
        session._recall_final_dedupe = seen
    expired = [key for key, until in seen.items() if float(until) <= stamp]
    for key in expired:
        seen.pop(key, None)
    until = stamp + _FINAL_DEDUPE_WINDOW_S
    event_key_hashed = "e:" + event_key if event_key else ""
    event_duplicate = bool(event_key_hashed and event_key_hashed in seen)
    last = getattr(session, "_recall_final_last", None)
    fingerprint_duplicate = bool(
        fingerprint
        and isinstance(last, tuple)
        and len(last) == 2
        and last[0] == fingerprint
        and float(last[1]) > stamp
    )
    if event_key_hashed:
        seen[event_key_hashed] = until
    if fingerprint:
        # Hashed fingerprint only — never transcript text. Updating this on
        # every final makes A → B → A a real sequence, while A → A → A is a
        # provider replay loop whose sliding window keeps refreshing.
        session._recall_final_last = (fingerprint, until)
    if len(seen) > _FINAL_DEDUPE_MAX:
        for key, _until in sorted(seen.items(), key=lambda pair: pair[1])[
            : len(seen) - _FINAL_DEDUPE_MAX
        ]:
            seen.pop(key, None)
    return event_duplicate or fingerprint_duplicate


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
        avatar = avatar_resolver.for_session(session)
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
            # Wake-word mode: stay SILENT until the actual answer — no "Sure —"
            # ack over a still-talking speaker (owner ask 2026-07-20). KEEP THIS
            # — a merge has reverted it 3x; the answer already waits for the
            # final (which lands after the pause), so no ack is needed here.
            and not _wake_required(avatar)
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
            and _should_backchannel(session, text, avatar)
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
            avatar = avatar_resolver.for_session(session)
            conversation_frame.observe_session_participant(
                session,
                avatar_name=avatar.name,
                participant_id=identity["id"],
                speaker_kind=identity["kind"],
                here=(event == "participant_events.join"),
            )
            if identity["kind"] != "agent":
                # Empty-room grace is a HUMAN-presence property: roster() counts
                # only humans, so gate the whole block on kind != agent. An agent
                # or bot join (bot reconnect, co-avatar) must NOT cancel a pending
                # finalize — a join never reschedules, so the meter would leak
                # until the call actually ends.
                if event == "participant_events.join":
                    # A human joined → invalidate the old deadline; a later
                    # last-human leave receives a full fresh grace.
                    _invalidate_empty_room_leave(session)
                elif not session.roster(avatar.name):
                    # Last human gone → grace-checked auto-finalize (meter safety).
                    _schedule_empty_room_leave(session)
                # ── footing: greet a late joiner by name ──
                # Only when the meeting is genuinely underway (start-of-call
                # joins greet each other anyway), only for NEW named humans,
                # and never over her own voice. Cheap acknowledgment has an
                # outsized social payoff (research doc).
                if (
                    event == "participant_events.join"
                    and settings.greet_joiners
                    and not _wake_required(avatar)  # wake-word mode: no unprompted greeting
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

    # Her own transcribed speech exits BEFORE the archive write: the verbatim
    # line was already recorded at dispatch (_make_avatar_speak), so archiving
    # the ASR rendition of the same words would double every agent turn. It
    # still must exit before barge-in, MeetingState, actions, readiness or
    # prompts.
    avatar = avatar_resolver.for_session(session)
    if _is_own_speech(avatar.name, speaker, speaker_kind):
        return JSONResponse({"ok": True, "spoke": False, "reason": "own speech"})
    if _is_duplicate_recall_final(
        session, capture_event_key, capture_fingerprint
    ):
        # Provider re-transcription/retry: do not archive it, reason over it,
        # capture it, or pay the response latency again.
        return JSONResponse(
            {"ok": True, "spoke": False, "reason": "duplicate final"}
        )
    session.add_utterance(
        speaker,
        text,
        participant_id=speaker_id,
        speaker_kind=speaker_kind,
    )

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

    # Shadow-only social state: observe the turn without changing any existing
    # speak/silence gate. The frame stores classifications, never utterance text.
    _frame_addressed_elsewhere = (
        not called and _addressed_elsewhere(session, avatar, text)
    )
    conversation_frame.observe_session_utterance(
        session,
        avatar_name=avatar.name,
        participant_id=speaker_id,
        speaker_kind=speaker_kind,
        text=text,
        addressed_avatar=avatar.name if called else "",
        addressed_participant_id="other" if _frame_addressed_elsewhere else "",
        action=(
            {"verb": "candidate"}
            if called and wants_action_capture(text)
            else None
        ),
    )

    # ── proactive intervention (fires once, as the meeting wraps up) ──
    if (
        settings.proactive_enabled
        and not _wake_required(avatar)  # wake-word mode: even the closing flag stays silent
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

    # ── live corrections on the ACTIVE draft (spec M, 2026-07-22) ──
    # "No, non venerdì — lunedì" / "lascia perdere" right after a capture must
    # MUTATE or WITHDRAW the one card she just queued — before this gate the
    # clarify/extend seams APPENDED the correction to the action text and the
    # wrong (or dead) card reached the dashboard. Runs BEFORE the clarify
    # block so a correction is never mistaken for the answer to her detail
    # question, and before the wake gates so a bare "no, not Anant — Marco"
    # works without re-saying her name. Only the asker being served (or a
    # by-name address) may amend the draft — cross-talk stays out.
    d_item, d_speaker = _active_draft(session)
    if d_item is not None and (called or (speaker_id and speaker_id == d_speaker)):
        if tools.is_draft_cancel(text):
            withdrawn = await run_in_threadpool(
                tools.withdraw_action_once, session, d_item
            )
            if withdrawn:
                store.settle_pending_action(
                    session, d_item, d_speaker, "withdrawn"
                )
            session.pending_clarify = None
            spoke = False
            if withdrawn:
                cancel_gen = store.bump_speech_generation(session)
                line = _line_for(text, _CANCEL_ACK_LINES, _CANCEL_ACK_LINES_IT)
                session.last_ack_at = time.time()
                spoke = await _make_avatar_speak(
                    session,
                    line,
                    force=True,
                    generation=cancel_gen,
                    audio=tts.cached_payload(line, _avatar_voice(session)),
                )
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": bool(spoke),
                    "action_cancelled": True,
                    "duplicate": not withdrawn,
                }
            )
        for corr_old, corr_new in tools.parse_corrections(text):
            corr_updates = tools.apply_correction(d_item, corr_old, corr_new)
            if corr_updates is None:
                continue  # OLD isn't in this draft — not a correction of it
            try:
                _revised, applied = await run_in_threadpool(
                    tools.revise_action_once,
                    session,
                    d_item,
                    corr_updates,
                    source_event_key=capture_event_key,
                    source_fingerprint=capture_fingerprint,
                )
            except outbox.ActionCaptureClosed:
                session.pending_clarify = None
                return JSONResponse(
                    {"ok": True, "spoke": False, "capture_rejected": "meeting_finalizing"}
                )
            # pending_clarify (if any) holds the SAME dict revise mutated in
            # place, so the clarify flow keeps working on the corrected draft.
            spoke = False
            if applied:
                corr_gen = store.bump_speech_generation(session)
                line = (
                    f"Ok — {corr_new}."
                    if sounds_italian(text)
                    else f"Got it — {corr_new}."
                )
                session.last_ack_at = time.time()
                spoke = await _make_avatar_speak(
                    session,
                    line,
                    force=True,
                    generation=corr_gen,
                    audio=tts.cached_payload(line, _avatar_voice(session)),
                )
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": bool(spoke),
                    "action_corrected": True,
                    "duplicate": not applied,
                }
            )

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
        if answered and _addressed_elsewhere(session, avatar, text):
            # The asker turned to another participant; never bind that turn.
            answered = False
        if answered and not tools.clarification_fragment_matches(
            text,
            tools.ask_kind(c_item.get("action") or ""),
            c_missing,
        ):
            # An unrelated question (for example an Asana snapshot lookup)
            # cannot become the pending task's title or description.
            answered = False
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
            still_missing = tools.missing_action_details(
                c_item.get("action") or "",
                kind=tools.ask_kind(c_item.get("action") or ""),
            )
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
        elif answered and tools.same_ask(c_item.get("action") or "", text):
            # The human REPEATED the ask (louder, or an ASR-mangled retry) —
            # that is not an answer to the clarify question. Extend the one
            # pending item and hold, instead of resolving + re-capturing a
            # duplicate card (live repro 2026-07-21: four cards for one email).
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
                session.pending_clarify = None
                return JSONResponse(
                    {"ok": True, "spoke": False, "capture_rejected": "meeting_finalizing"}
                )
            still_missing = tools.missing_action_details(
                c_item.get("action") or "",
                kind=tools.ask_kind(c_item.get("action") or ""),
            )
            if still_missing:
                session.pending_clarify = (
                    c_item, c_speaker, time.time(), still_missing,
                    c_event_key, c_fingerprint,
                )
                spoke = False
                if still_missing != c_missing:
                    # The retry filled a slot — ask only for what's left.
                    restate_gen = store.bump_speech_generation(session)
                    line = _clarify_line(text, still_missing)
                    session.last_ack_at = time.time()
                    session.last_clarify_nudge_at = time.time()
                    spoke = await _make_avatar_speak(
                        session,
                        line,
                        force=True,
                        generation=restate_gen,
                        audio=tts.cached_payload(line, _avatar_voice(session)),
                    )
                elif (
                    time.time()
                    - getattr(session, "last_clarify_nudge_at", 0.0)
                    > 20.0
                ):
                    # NEVER hold a clarify in silence (live 2026-07-21: a
                    # repeated ask with unchanged gaps read as "she stopped
                    # responding"). One rate-limited reminder of what's
                    # still needed — a nudge, not a nag.
                    nudge_gen = store.bump_speech_generation(session)
                    line = _clarify_line(text, still_missing)
                    session.last_ack_at = time.time()
                    session.last_clarify_nudge_at = time.time()
                    spoke = await _make_avatar_speak(
                        session,
                        line,
                        force=True,
                        generation=nudge_gen,
                        audio=tts.cached_payload(line, _avatar_voice(session)),
                    )
                return JSONResponse(
                    {
                        "ok": True,
                        "spoke": bool(spoke),
                        "action_capture": True,
                        "restated": True,
                        "clarifying": still_missing,
                    }
                )
            answered = True  # the retry completed the ask — resolve below
            text_is_details = False
        else:
            text_is_details = answered and not tools.is_detail_skip(text)
        if answered or expired:
            if text_is_details:
                try:
                    # A clarify answer fills ONE labelled required slot on the
                    # same card. Revalidate below before any approval: one reply
                    # must never make unrelated missing fields disappear.
                    updates = tools.fold_action_details(c_item, text, c_missing)
                    c_item, _ = await run_in_threadpool(
                        tools.revise_action_once,
                        session,
                        c_item,
                        updates,
                        source_event_key=capture_event_key,
                        source_fingerprint=capture_fingerprint,
                    )
                except outbox.ActionCaptureClosed:
                    session.pending_clarify = None
                    return JSONResponse(
                        {"ok": True, "spoke": False, "capture_rejected": "meeting_finalizing"}
                    )
            remaining = tools.missing_action_details(
                c_item.get("action") or "",
                kind=tools.ask_kind(c_item.get("action") or ""),
            )
            store.bind_pending_action(
                session,
                c_item,
                c_speaker,
                tools.ask_kind(c_item.get("action") or ""),
                remaining,
                collected_parameters=tools.collected_action_parameters(
                    c_item.get("action") or ""
                ),
                question=_clarify_line(text, remaining) if remaining else "",
                source_event_key=c_event_key,
                source_fingerprint=c_fingerprint,
            )
            # Owner/project/due/description improve an Asana card but are not
            # provider-required. After the asker replies once, explicitly skips,
            # or lets the window expire, those optional enrichments cannot hold
            # or auto-invalidate the captured task. task_name, email and calendar
            # slots remain hard requirements and are re-asked below.
            _optional_task_slots = {"owner", "project", "due", "description"}
            if (
                remaining
                and set(remaining).issubset(_optional_task_slots)
                and (answered or expired)
            ):
                remaining = []
            if remaining:
                if answered:
                    # A skip such as "that's it" cannot waive a provider-
                    # required field. Keep the one captured card open and ask
                    # only the next required detail.
                    session.pending_clarify = (
                        c_item, c_speaker, time.time(), remaining,
                        c_event_key, c_fingerprint,
                    )
                    clar_gen = store.bump_speech_generation(session)
                    line = _clarify_line(text, remaining)
                    session.last_ack_at = time.time()
                    session.last_clarify_nudge_at = time.time()
                    spoke = await _make_avatar_speak(
                        session,
                        line,
                        force=True,
                        generation=clar_gen,
                        audio=tts.cached_payload(line, _avatar_voice(session)),
                    )
                    return JSONResponse(
                        {
                            "ok": True,
                            "spoke": bool(spoke),
                            "action_capture": True,
                            "clarifying": remaining,
                        }
                    )
                # Expiry releases the conversation but never auto-approves an
                # invalid write. The captured card remains available for the
                # dashboard's needs-details form.
                session.pending_clarify = None
            else:
                _settle_pending_action(session, c_item, c_speaker, "proposed")
                if settings.voice_consent_writes:
                    asyncio.create_task(
                        run_in_threadpool(cedric.voice_approve, session, c_item)
                    )
                if answered:
                    clar_gen = store.bump_speech_generation(session)
                    line = (
                        _line_for(text, _VOICE_LINES, _VOICE_LINES_IT)
                        if settings.voice_consent_writes
                        else _queue_line_for(text, c_item)
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
                        {
                            "ok": True,
                            "spoke": bool(spoke),
                            "action_capture": True,
                            "clarified": True,
                        }
                    )
            # expired with missing fields: current line continues below.

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
            # Absolute cap: every glued fragment used to refresh the 4s window,
            # so continuous speech chained extensions forever and the avatar
            # read as mute (live 2026-07-21 — "why did she stop talking").
            # A real ASR split resolves within seconds; 8s from the FIRST
            # capture is plenty, then the conversation goes back to normal.
            and time.time() - getattr(session, "last_capture_first_ts", p_ts) < 8.0
            and is_capture_continuation(text)
            # A follow-up that is ITSELF a complete new ask ("Also create a
            # task to email Duccio…") is a NEW action, never a continuation —
            # gluing it merged two distinct instructions into one monster item
            # and silently swallowed the second one's confirmation (round-4
            # live repro 2026-07-21). It falls through to the capture branch.
            and not wants_action_capture(text)
            # A line opening a turn aimed at another participant is never an
            # ASR split of the ask (live repro 2026-07-22: "Ducho, do you
            # wanna discuss something else?" glued onto the calendar card).
            and not _addressed_elsewhere(session, avatar, text)
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
        # A same-intent RETRY of the captured ask must keep the window alive —
        # the capture gate below merges it into the existing card instead of
        # minting another (2026-07-21: four cards for one email). Everything
        # else still closes the continuation window here.
        if not tools.same_ask(str(p_item.get("action") or ""), text):
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
            and _human_count(session, avatar) <= 1
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
        # A REPEATED "can you leave?" landing during the goodbye grace must not
        # queue a second goodbye (live 2026-07-23: asked twice → "Sure — bye
        # everyone!" then "Okay, leaving now. Bye!"). First command owns the
        # exit; retries answer quietly while it finishes.
        if getattr(session, "leaving_now", False):
            return JSONResponse(
                {"ok": True, "spoke": False, "left": True,
                 "reason": "leave_command_duplicate"}
            )
        session.leaving_now = True
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
        pending_kind = session.pending_contribution_kind
        session.hand_last_ignored = False  # the room engaged her: no back-off
        deliver_pending = bool(
            pending
            and (
                detect_invite(question)
                or _address_is_bare(avatar, text)
                or (
                    pending_kind == "search"
                    and _is_search_result_request(question or text)
                )
            )
        )
        if deliver_pending:
            await _lower_hand(session)
            turn_gen = store.bump_speech_generation(session)
            spoke = await _speak_with_audio(
                session, pending, force=True, generation=turn_gen, prev=None
            )
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": bool(spoke),
                    "hand_delivered": True,
                    "search_result": pending_kind == "search",
                }
            )
        # A requested search result remains queued across an unrelated direct
        # question; unsolicited hand points keep the historical drop behavior.
        if pending_kind != "search":
            await _lower_hand(session)

    # The asker may check back while the provider call is still running. Say
    # what is true and keep the original job alive; never answer the lookup from
    # internal documents just because its web result is not ready yet.
    if called and session.search_inflight_at and _is_search_result_request(
        question or text
    ):
        if time.time() - session.search_inflight_at <= 120.0:
            line = _line_for(
                question or text, _SEARCH_STILL_LINES, _SEARCH_STILL_LINES_IT
            )
            gen = store.bump_speech_generation(session)
            spoke = await _make_avatar_speak(
                session, line, force=True, generation=gen,
                audio=tts.cached_payload(line, _avatar_voice(session)),
            )
            return JSONResponse(
                {"ok": True, "spoke": bool(spoke), "search_inflight": True}
            )
        session.search_inflight_at = 0.0
        session.search_inflight_query = ""

    # ── in-meeting browser view (Sable B2/B3) ──
    # Addressed "open Asana and show me…" → open a live browser view on her
    # tile. Off by default (browser_meeting_trigger_enabled). Everything heavy
    # runs in a fire-and-forget task off this transcript→speak path; the ack
    # speak is the only thing on the turn. A browse fault never touches the
    # meeting (browser_meeting swallows its own errors).
    # In a 1:1 room (only one human present) a browse request doesn't need her
    # name — "open Asana and show me" is unambiguously for her, so the
    # conversation stays fluid. In a group she still needs to be addressed
    # (mirrors the leave-command 1:1 relaxation). The utterance is `question`
    # when named, else the whole line.
    _browse_solo = _human_count(session, avatar) <= 1
    if (called or _browse_solo) and browser_meeting.trigger_enabled():
        _ask = question if called else text
        if detect_browse_dismiss(_ask):
            # Explicit stop → advance the walkthrough epoch so any running
            # walkthrough cancels (it is not gated on speech_generation).
            _BROWSE_EPOCH[session.bot_id] = (
                _BROWSE_EPOCH.get(session.bot_id, 0) + 1)

            async def _hide_browser() -> None:
                closed = await run_in_threadpool(
                    browser_meeting.close_for_meeting, session.bot_id)
                if closed:
                    await _send_avatar_control(
                        session, {"type": "browser_view_hide"})
            asyncio.create_task(_hide_browser())
            return JSONResponse({"ok": True, "spoke": False, "browse_hide": True})
        browse_ok, browse_site, browse_task = detect_browse_intent(_ask)
        # PII-safe telemetry: booleans + labels only, NEVER the utterance —
        # so a non-firing trigger is diagnosable (verb vs site) without logging
        # transcript content.
        _bv, _bs = browse_signal(_ask)
        print(f"[browse] called={called} solo={_browse_solo} "
              f"matched={browse_ok} verb={_bv} site_or_surface={_bs} "
              f"site={browse_site!r} task={browse_task!r}", flush=True)
        if browse_ok:
            # Debounce BEFORE claiming the epoch: if a browse is already opening
            # for this meeting, the human is almost always re-asking because the
            # slow (~15-20s) open hasn't shown anything yet. Ignore it so it does
            # not advance the epoch and cancel the in-flight walkthrough.
            if _BROWSE_INFLIGHT.get(session.bot_id, 0.0) > time.monotonic():
                print("[browse] debounced (open in-flight)", flush=True)
                return JSONResponse(
                    {"ok": True, "spoke": False, "browse_inflight": True})
            spoken_name = browser_meeting.site_spoken_name(browse_site)
            loop = asyncio.get_running_loop()
            # Anchor every browse speak to THIS turn's generation: a barge-in
            # or "stop" bumps the generation, which (a) drops queued/late
            # narration in _make_avatar_speak and (b) cancels the walkthrough
            # loop itself — she never talks over a human who took the floor.
            browse_gen = store.bump_speech_generation(session)
            # Claim the walkthrough epoch for THIS ask. Advancing it also cancels
            # any walkthrough still running from a previous ask. Unlike
            # speech_generation this is NOT bumped by ambient utterances, so the
            # walkthrough survives a chatty 1:1 and the browser-open backlog.
            browse_epoch = _BROWSE_EPOCH.get(session.bot_id, 0) + 1
            _BROWSE_EPOCH[session.bot_id] = browse_epoch

            # Self-service connect: no saved login for this site → put a sign-in
            # link in the meeting chat (the tile is one-way video, so the human
            # signs in from their OWN browser), then remember it. Everything off
            # the speak path; a failure never touches the meeting.
            if not browser_meeting.has_identity(session.org_id, browse_site):
                async def _connect_flow() -> None:
                    res = await run_in_threadpool(
                        browser_meeting.begin_connect, session.org_id,
                        browse_site, session.bot_id)
                    if not res.get("ok"):
                        await _make_avatar_speak(
                            session,
                            f"I couldn't start the {spoken_name} sign-in just "
                            "now.", force=True, generation=browse_gen)
                        return
                    await run_in_threadpool(
                        recall_client.send_chat_message, session.bot_id,
                        f"Sign in to {spoken_name} here so I can show it to "
                        f"you — it's private, I only keep the session, never "
                        f"your password: {res['login_url']}")
                    await _make_avatar_speak(
                        session,
                        f"I don't have your {spoken_name} login yet. I've put a "
                        "sign-in link in the meeting chat — open it, log in, and "
                        "I'll remember it for next time.", force=True,
                        generation=browse_gen)
                    for _ in range(60):  # poll ~6 minutes
                        await asyncio.sleep(6)
                        state = await run_in_threadpool(
                            browser_meeting.poll_connect, session.bot_id)
                        if state == "logged_in":
                            await run_in_threadpool(
                                browser_meeting.finish_connect, session.bot_id)
                            await _make_avatar_speak(
                                session,
                                f"Great — I'm connected to {spoken_name} now. "
                                "Ask me again and I'll show you.", force=True)
                            return
                        if state in ("gone", "none"):
                            return
                    await run_in_threadpool(
                        browser_meeting.cancel_connect, session.bot_id)

                asyncio.create_task(_connect_flow())
                return JSONResponse(
                    {"ok": True, "spoke": False, "browse_connect": True})

            async def _open_browser() -> None:
                # Immediate acknowledgement BEFORE the slow (~15-20s) open, so the
                # human knows it is happening and does not re-ask through the wait
                # (the re-asks are what cancelled the walkthrough). Un-gated so it
                # plays regardless of ambient chatter.
                await _make_avatar_speak(
                    session,
                    (f"One moment — opening {spoken_name} to show you."
                     if browse_task else f"One moment — opening {spoken_name}."),
                    force=True)
                result = await run_in_threadpool(
                    browser_meeting.open_for_meeting, session.org_id,
                    avatar_key=avatar.id, site_label=browse_site,
                    meeting_ref=session.bot_id)
                print(f"[browse-open] site={browse_site} ok={result.get('ok')} "
                      f"reason={result.get('reason')} "
                      f"logged_in={result.get('logged_in')} "
                      f"task={browse_task!r}", flush=True)
                if not result.get("ok"):
                    _BROWSE_INFLIGHT.pop(session.bot_id, None)
                    await _make_avatar_speak(
                        session, f"I couldn't open {spoken_name} just now.",
                        force=True, generation=browse_gen)
                    return
                # View is on the tile now — clear the debounce so a genuine
                # follow-up ("...now create a task") is honored via reuse.
                await _send_avatar_control(
                    session, {"type": "browser_view", "url": result["url"]})
                _BROWSE_INFLIGHT.pop(session.bot_id, None)
                if not result.get("logged_in"):
                    await _make_avatar_speak(
                        session,
                        f"I don't have a saved {spoken_name} login yet, so "
                        f"here's the {spoken_name} help center.",
                        force=True, generation=browse_gen)
                    return
                await _make_avatar_speak(
                    session,
                    (f"Sure — here's how you'd do that in {spoken_name}."
                     if browse_task else f"Opening {spoken_name} for you."),
                    force=True, generation=browse_gen)
                # Guided how-to: drive the visual planner, narrating each step.
                # narrate() bridges the threadpool coordinator back onto the
                # event loop; fire-and-forget so it never blocks a step. Every
                # line carries browse_gen, so a stop silences it server-side.
                if browse_task:
                    # Cancel the walkthrough ONLY on a new browse ask or an
                    # explicit dismiss (the per-meeting epoch) — never on the
                    # ambient speech-generation churn of a 1:1, which drained a
                    # turn backlog the instant we awaited and cancelled the recipe
                    # at step 0. Narration is sent un-gated (generation=None) so it
                    # plays through that churn; it stops when the epoch advances.
                    def _walk_cancelled() -> bool:
                        return (_BROWSE_EPOCH.get(session.bot_id, 0)
                                != browse_epoch)

                    def _narrate(narration_line: str) -> None:
                        if _walk_cancelled():
                            return
                        try:
                            asyncio.run_coroutine_threadsafe(
                                _make_avatar_speak(
                                    session, narration_line, force=True), loop)
                        except Exception:  # noqa: BLE001
                            pass
                    walk = await run_in_threadpool(
                        browser_meeting.run_walkthrough, session.org_id,
                        result["session_id"], site_label=browse_site,
                        task_key=browse_task, on_narrate=_narrate,
                        cancel=_walk_cancelled)
                    if walk.get("closing") and not _walk_cancelled():
                        await _make_avatar_speak(
                            session, walk["closing"], force=True)

            # Mark this meeting's browse as opening (debounce re-asks) with an
            # auto-expiring deadline as a backstop; _open_browser clears it the
            # moment the view is on the tile or the open fails.
            _BROWSE_INFLIGHT[session.bot_id] = time.monotonic() + 40.0
            asyncio.create_task(_open_browser())
            return JSONResponse(
                {"ok": True, "spoke": False, "browse": True, "task": browse_task})

    # ── footing: quiet-participant nudge (fires once, at wrap-up) ──
    # She knows who is in the room (roster) and who has spoken (transcript).
    # As the meeting wraps up — and she wasn't addressed directly — invite ONE
    # silent participant in: the verbal analogue of turn-yielding gaze, which
    # no shipping meeting-AI does by voice (research doc). Placed after the
    # proactive intervention (critical process gaps win the wrap-up slot).
    if (
        settings.quiet_nudge_enabled
        and not _wake_required(avatar)  # wake-word mode: no unprompted nudges
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

    # Tool-domain addressing (owner 2026-07-24, 3-person call): "can you check
    # my calendar and my next meeting?" was IGNORED because the speaker didn't
    # say her name. A personal-workspace read, an action ask, or a capability
    # question can only be aimed at the assistant — treat it as addressed even
    # without the name, in every gating decision below (wake, hand-raise,
    # cooldown). Human-to-human chatter never matches these shapes.
    if not called and (
        is_tool_domain_ask(text) or capabilities.is_capability_question(text)
    ):
        called = True

    if _wake_required(avatar, session) and not called:
        # Per-avatar wake-word mode (falls back to the global flag): she was
        # not addressed by name — stay silent, UNLESS this is a follow-up
        # right after her own answer (handled below: a reply to her turn).
        followup_ok = (
            settings.followup_window_seconds > 0
            and (time.time() - session.last_spoke_at) < settings.followup_window_seconds
            and _followup_shape_ok(text)
            and _followup_speaker_ok(session, speaker_id)
        )
        if not followup_ok:
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
        and _followup_shape_ok(text)
        # Window belongs to the participant she just answered: a DIFFERENT
        # speaker's bare line is not a follow-up — it degrades to the normal
        # room-open path below (deference + SKIP), never a fast-path reply.
        and _followup_speaker_ok(session, speaker_id)
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

    # This turn was admitted (called / follow-up / room-open past deference):
    # the asker becomes the follow-up window's interlocutor — THEIR next bare
    # line rides the window (_followup_speaker_ok). Runtime-only, like
    # last_addressed: a 15s dialogue affinity, not persisted state.
    session.followup_owner = (speaker_id, time.time())

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
    # Only when addressed by name — EXCEPT in a true 1:1, where an instruction
    # is unambiguously for her even without her name (same roster rule as the
    # _wake_required fluid relaxation; owner ask 2026-07-21, round-3 test: solo
    # instructions were never captured, so no confirmation ever spoke and every
    # ask fell to the summarizer as a mere "goal"), and EXCEPT a follow-up
    # inside the window from the speaker she's serving ("crea una task" right
    # after her answer IS for her — spec 2026-07-22; the window is already
    # shape- and speaker-bound). Groups keep name-required capture otherwise:
    # an unaddressed "someone should send X" stays the summarizer's job at
    # finalize.
    if ((called or followup or _human_count(session, avatar) <= 1)
            and wants_action_capture(question)
            and not wants_web_search(question)
            # A 'show me Asana / give me a tour' ask is a LIVE thing the
            # avatar does now (browser walkthrough) — never a post-meeting
            # to-do. Keep it off the capture seam.
            and not detect_browse_intent(question)[0]):
        if tools.is_orphan_action_fragment(question):
            # A field-only fragment without a compatible pending parent is not
            # an action and cannot become a tracked-only approval card.
            line = (
                "Which action should that update?"
                if not sounds_italian(question)
                else "A quale azione si riferisce?"
            )
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
                    "action_capture": False,
                    "needs_details": True,
                }
            )
        # Same-intent retry damping (live repro 2026-07-21: four cards for
        # one email). A re-ask of the capture she is still CLARIFYING extends
        # that one item and keeps the clarify pending — reachable here when
        # the retry was addressed by name (unaddressed retries hit the same
        # logic in the clarify block above). A re-ask of a capture RESOLVED
        # in the last _SAME_ASK_WINDOW_S extends the existing card instead of
        # minting another.
        pend = getattr(session, "pending_clarify", None)
        if pend is not None and tools.same_ask(pend[0].get("action") or "", question):
            try:
                p_item, _ = await run_in_threadpool(
                    tools.extend_action_once,
                    session,
                    pend[0],
                    question.strip(),
                    source_event_key=capture_event_key,
                    source_fingerprint=capture_fingerprint,
                )
            except outbox.ActionCaptureClosed:
                session.pending_clarify = None
                return JSONResponse(
                    {"ok": True, "spoke": False, "capture_rejected": "meeting_finalizing"}
                )
            still = tools.missing_action_details(
                p_item.get("action") or "",
                kind=tools.ask_kind(p_item.get("action") or ""),
            )
            if still:
                session.pending_clarify = (
                    p_item, pend[1], time.time(), still, pend[4], pend[5],
                )
                spoke = False
                if still != pend[3]:
                    line = _clarify_line(question, still)
                    session.last_ack_at = time.time()
                    session.last_clarify_nudge_at = time.time()
                    spoke = await _make_avatar_speak(
                        session,
                        line,
                        force=True,
                        generation=turn_gen,
                        audio=tts.cached_payload(line, _avatar_voice(session)),
                    )
                elif (
                    time.time()
                    - getattr(session, "last_clarify_nudge_at", 0.0)
                    > 20.0
                ):
                    # Same no-silent-hold rule as the unaddressed-retry seam
                    # above: remind what's still missing instead of muting.
                    line = _clarify_line(question, still)
                    session.last_ack_at = time.time()
                    session.last_clarify_nudge_at = time.time()
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
                        "restated": True,
                        "clarifying": still,
                    }
                )
            session.pending_clarify = None
            if settings.voice_consent_writes:
                asyncio.create_task(
                    run_in_threadpool(cedric.voice_approve, session, p_item)
                )
            line = _queue_line_for(question, p_item)
            session.last_ack_at = time.time()
            spoke = await _make_avatar_speak(
                session,
                line,
                force=True,
                generation=turn_gen,
                audio=tts.cached_payload(line, _avatar_voice(session)),
            )
            return JSONResponse(
                {"ok": True, "spoke": bool(spoke), "action_capture": True, "restated": True}
            )
        recent = getattr(session, "last_capture", None)
        if (
            recent is not None
            and time.time() - recent[2] < _SAME_ASK_WINDOW_S
            and tools.same_ask(recent[0].get("action") or "", question)
        ):
            try:
                r_item, _ = await run_in_threadpool(
                    tools.extend_action_once,
                    session,
                    recent[0],
                    question.strip(),
                    source_event_key=capture_event_key,
                    source_fingerprint=capture_fingerprint,
                )
            except outbox.ActionCaptureClosed:
                session.last_capture = None
                return JSONResponse(
                    {"ok": True, "spoke": False, "capture_rejected": "meeting_finalizing"}
                )
            session.last_capture = (
                r_item, recent[1], recent[2], recent[3], recent[4],
            )
            line = _line_for(question, _ALREADY_LINES, _ALREADY_LINES_IT)
            session.last_ack_at = time.time()
            spoke = await _make_avatar_speak(
                session,
                line,
                force=True,
                generation=turn_gen,
                audio=tts.cached_payload(line, _avatar_voice(session)),
            )
            return JSONResponse(
                {"ok": True, "spoke": bool(spoke), "action_capture": True, "merged": True}
            )
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
            session.last_capture_first_ts = time.time()
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
        # continuation check after wake detection). The FIRST-capture clock
        # anchors the absolute glue cap — extensions never reset it.
        session.last_capture_first_ts = time.time()
        session.last_capture = (
            item,
            speaker_id,
            time.time(),
            capture_event_key,
            capture_fingerprint,
        )
        if session.speech_generation != turn_gen:  # barge-in since the final landed
            return JSONResponse({"ok": True, "spoke": False, "interrupted": True})
        # A new ask releases the old conversational slot, but an incomplete
        # action remains needs_details and is NEVER auto-approved.
        stale = getattr(session, "pending_clarify", None)
        if stale is not None:
            store.settle_pending_action(session, stale[0], stale[1], "needs_details")
            session.pending_clarify = None
        missing = tools.missing_action_details(
            item.get("action") or "",
            kind=tools.ask_kind(item.get("action") or ""),
        )
        if settings.clarify_before_create and missing:
            # The ask lacks what a well-filed task needs — Petra ASKS instead
            # of filing an orphan. The approval is held until the asker's
            # reply resolves it (clarify block above), or the window lapses.
            line = _bind_pending_action(
                session,
                item,
                speaker_id,
                time.time(),
                missing,
                capture_event_key,
                capture_fingerprint,
                question,
            )
            session.last_ack_at = time.time()
            session.last_clarify_nudge_at = time.time()
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
            line = _queue_line_for(question, item)
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
        # Wake-word mode stays silent until the real answer (owner ask
        # 2026-07-20; KEEP — reverted 3x by merges). The final lands after the
        # speaker's pause, so the answer already waits for them to finish.
        and not _wake_required(avatar)
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
    # Final received → begin building the answer. Anchors the per-stage latency
    # line emitted after the stream (durations only — never transcript text).
    _t_turn0 = time.perf_counter()
    history = session.recent_transcript(n=8)
    # Compact questions-only recap so "what did I ask you before?" reaches past
    # the 8-line window (live 2026-07-23: recalled only the last two). Short by
    # construction — a few tokens — and paired with a smaller own_recent below,
    # so the prompt does not grow net.
    _user_questions = session.recent_user_questions(6)

    # Context-bound ASR repair for mis-heard app names ("what's in my zone?" →
    # "what's in my Asana?" ONLY when Asana was just discussed). Scoped to the
    # ANSWER text; the capture path (text) is deliberately left untouched.
    if question:
        question = capabilities.repair_asr(question, history)

    # ── on-demand snapshot pull (owner 2026-07-24) ──
    # "Petra, can you get the snapshot now?" — the capability answer promised
    # "I can pull one" but NO pull existed: the brief only ever loaded at join,
    # so a failed join-fetch left her snapshotless all meeting (3-person call:
    # she refused Asana reads for ten minutes while claiming she could pull).
    # Deterministic: ack, fetch the brief in the threadpool (1-3s), fold it
    # into memory_brief, flip the flag (the capability cache recomputes on it),
    # and confirm. Never the language model, never a web search.
    if _SNAPSHOT_PULL.search(question or text):
        _pull_q = question or text
        _is_mail = bool(re.search(r"\b(inbox|gmail|e-?mail|posta)\b", _pull_q, re.I))
        _noun = "Gmail inbox" if _is_mail else "Asana workspace"
        _pull_gen = store.bump_speech_generation(session)
        session.last_ack_at = time.time()
        _ack = f"One sec — pulling your {_noun} snapshot."
        await _make_avatar_speak(
            session, _ack, force=True, generation=_pull_gen,
            audio=tts.cached_payload(_ack, _avatar_voice(session)),
        )
        if _is_mail:
            _pulled = await run_in_threadpool(
                google_client.gmail_inbox_brief, session.org_id
            )
            _label = "[Gmail inbox — snapshot pulled mid-meeting (headers only)]"
        else:
            _pulled = await run_in_threadpool(
                asana_client.workspace_brief, session.org_id
            )
            _label = "[Asana workspace — snapshot pulled mid-meeting]"
        if _pulled:
            # Replace any earlier section of the same family (join-time or a
            # previous pull) so repeated pulls never bloat the prompt.
            _mem = re.sub(
                rf"\[{'Gmail inbox' if _is_mail else 'Asana workspace'}[^\]]*\]\n.*?(?:\n\n|\Z)",
                "", session.memory_brief or "", flags=re.S,
            )
            session.memory_brief = f"{_label}\n{_pulled}\n\n{_mem}"
            if _is_mail:
                session.gmail_brief_loaded = True
            else:
                session.asana_brief_loaded = True
            _done = (
                f"Done — I've got your {_noun} now. Ask away."
            )
        else:
            _done = (
                f"I couldn't reach your {_noun} right now — the connection "
                "may need a look on the dashboard. I can still capture items "
                "for approval."
            )
        _spoke_pull = await _make_avatar_speak(
            session, _done, force=True,
            generation=store.bump_speech_generation(session),
        )
        print(f"[latency] snapshot pull family="
              f"{'gmail' if _is_mail else 'asana'} ok={bool(_pulled)}",
              flush=True)
        return JSONResponse(
            {"ok": True, "spoke": bool(_spoke_pull), "snapshot_pull": True,
             "loaded": bool(_pulled)}
        )

    # ── deterministic capability answer (grounding, never the language model) ──
    # "which tools can you use / are you connected to Asana / what actions can
    # you do in Gmail / do you have a snapshot of my Asana" are answered from the
    # SAME connection state the dashboard + executors use (capabilities.snapshot),
    # not from web search or a gemma-improvised roster. Live 2026-07-23: within
    # one meeting Petra claimed Asana connected, then 'no access', then 'can't
    # access external apps', and web-searched her own Gmail actions. Runs BEFORE
    # the web-search and ears branches so a capability/workspace question can
    # NEVER take an ungrounded path — even when reply mode is enabled.
    if capabilities.is_capability_question(question or text):
        _cap_t0 = time.perf_counter()
        _cap_snap = await run_in_threadpool(
            capabilities.cached_snapshot, avatar, session.org_id, session
        )
        _cap_line = capabilities.answer(question or text, _cap_snap)
        _cap_gen = store.bump_speech_generation(session)
        session.last_ack_at = time.time()
        _cap_spoke = await _make_avatar_speak(
            session, _cap_line, force=True, generation=_cap_gen,
            audio=tts.cached_payload(_cap_line, _avatar_voice(session)),
        )
        print(
            f"[latency] capability answer total="
            f"{int((time.perf_counter() - _cap_t0) * 1000)}ms web_search=no",
            flush=True,
        )
        return JSONResponse(
            {"ok": True, "spoke": bool(_cap_spoke), "capability_answer": True,
             "web_search": False}
        )

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
    # Repeat guard: a short follow-up fragment ("e prima?", "and before?") must
    # not make her re-speak the previous multi-sentence answer verbatim. The
    # speak layer already dedupes an UNADDRESSED repeat, but a follow-up is
    # `called`/`followup` → force=True → it bypasses that. So on a short
    # follow-up we compare each sentence to her last turn and drop near-verbatim
    # replays (reusing similar_contribution). Dropped == suppressed → the
    # existing "duplicate answer suppressed" terminal handles the silence.
    _dropped_dup = False
    _last_agent_line = session.last_agent_line()
    _repeat_guard = (
        bool(_last_agent_line)
        and (called or followup)
        and len((question or text).split()) <= 4
    )
    speak_tasks: list[asyncio.Task] = []
    prev_task: asyncio.Task | None = None
    _search_turn = wants_web_search(question or text)
    # A clarify is pending → this turn is almost certainly the ANSWER to it
    # ("Between me and Duccio today?" hit the 'today' freshness trigger and
    # web-searched, then the search model honestly declared it had no calendar
    # access — live 2026-07-24). While a capture waits on details, fragments
    # never route to the public web; the grounded path (and the fold) own them.
    if _search_turn and getattr(session, "pending_clarify", None) is not None:
        _search_turn = False
    _search_announce_seen = False
    _late_search_sentences: list[str] = []
    if _search_turn:
        session.search_inflight_at = time.time()
        session.search_inflight_query = question or text
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
    # Knowledge-graph recall (graphiti, optional/off by default): when the PM
    # avatar is addressed, hybrid-search the org's temporal graph for THIS
    # question and fold the facts into her grounding ahead of the flat snapshot.
    # Gated on the join-cached asana_live flag (never a live DB read); recall()
    # is timeout-bounded and never inits on the hot path. (KEEP — reverted by
    # merges repeatedly.)
    if graphiti_client.enabled() and getattr(session, "asana_live", False):
        _kg = await graphiti_client.recall(session.org_id, question or text)
        if _kg:
            memory = (
                "[Knowledge graph — facts relevant to this question]\n"
                + _kg + "\n\n" + (memory or "")
            )
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
                # Bounded: the gist of her last 2 turns, clipped — not 3 full
                # multi-sentence answers. #421 added this block unclipped, which
                # is the input-token cost that shows up as first-token latency
                # (see the [latency] turn line). The clip keeps the anti-repeat
                # signal without the tokens.
                own_recent=session.recent_agent_lines(n=2, max_chars=140),
                questions="\n".join(_user_questions),
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
            # The first yielded search line is the immediate "looking it up"
            # announcement. If the room continues while the provider works, keep
            # collecting the eventual result instead of cancelling it with the
            # ordinary speech-generation guard.
            is_search_announce = _search_turn and not _search_announce_seen
            if is_search_announce:
                _search_announce_seen = True
            if session.speech_generation != turn_gen:
                if _search_turn and not is_search_announce:
                    _late_search_sentences.append(sentence)
                    continue
                interrupted = True
                break
            if hand_mode:
                hand_sentences.append(sentence)
                continue
            # Repeat guard (short follow-up only): drop a sentence that re-speaks
            # her last turn near-verbatim, rather than letting `force=called`
            # push it through the speak-layer dedupe. A search-announce line is
            # never a replay, so it is exempt. All sentences dropping → nothing
            # spoken + suppressed_any → the "duplicate answer suppressed" branch.
            if (
                _repeat_guard
                and not is_search_announce
                and similar_contribution(sentence, _last_agent_line, threshold=0.8)
            ):
                _dropped_dup = True
                suppressed_any = True
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
        if _search_turn:
            session.search_inflight_at = 0.0
            session.search_inflight_query = ""
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

    # ── per-stage latency telemetry (durations + sizes + flags only; NO text) ──
    # The normal-path breakdown #421 lacked. Read off _answer_meta after the
    # stream: retrieve (route→retrieval), first_token (retrieval→first token),
    # plus prompt-size fields so the own_recent token cost is directly visible.
    # asr_route = the main-side overhead before the model (history/questions
    # build, ASR repair, capability-miss, roster, graphiti). Capability turns
    # log their own line and return earlier, so this is the ordinary path only.
    print(
        f"[latency] turn total={int((time.perf_counter() - _t_turn0) * 1000)}ms "
        f"asr_route={int((_t_wake - _t_turn0) * 1000)}ms "
        f"retrieve={'skip' if _answer_meta.get('rag_skipped') else str(int(_answer_meta.get('retrieve_ms', 0)))+'ms'} "
        f"first_token={int(_answer_meta.get('first_token_ms', 0))}ms "
        f"web_search={'yes' if _answer_meta.get('web_search') else 'no'} "
        f"cancelled={'yes' if interrupted else 'no'} "
        f"duplicated={'yes' if _dropped_dup else 'no'} "
        f"hist_chars={_answer_meta.get('hist_chars', 0)} "
        f"own_chars={_answer_meta.get('own_chars', 0)} "
        f"q_chars={_answer_meta.get('questions_chars', 0)} "
        f"model={_answer_meta.get('model', '')}",
        flush=True,
    )

    if _search_turn:
        session.search_inflight_at = 0.0
        session.search_inflight_query = ""

    if _late_search_sentences:
        # The room moved on during the lookup. Finish the initial announce, then
        # signal readiness instead of speaking over a multi-person discussion.
        if speak_tasks:
            results = await asyncio.gather(*speak_tasks, return_exceptions=True)
            spoke_any = any(r is True for r in results)
        contribution = " ".join(_late_search_sentences).strip()[:600]
        if contribution and len(roster) >= 2:
            session.pending_contribution = contribution
            session.pending_contribution_kind = "search"
            session.pending_contribution_query = question or text
            await _raise_hand(session, avatar, heard=question or text)
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": spoke_any,
                    "search_ready": True,
                    "hand_raised": True,
                }
            )
        if contribution:
            current_gen = session.speech_generation
            spoke = await _speak_with_audio(
                session, contribution, force=True,
                generation=current_gen, prev=None,
            )
            return JSONResponse(
                {"ok": True, "spoke": bool(spoke), "search_ready": True}
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
                enabled=settings.hand_raise_interject_when_confident
                and not _wake_required(avatar),  # wake-word mode: never interject
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
