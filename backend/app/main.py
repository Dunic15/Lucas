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
import json
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
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
    cedric,
    drive_client,
    granola_client,
    actions,
    gmail_watcher,
    autopilot,
    gpu_runtime,
    ledger,
    meeting_state,
    org_api,
    tools,
    tts,
)
from .brain import (
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
    proactive_flag,
    effective_provider,
)
from .config import settings
from .decision import (
    addressed_to_other,
    detect_wake,
    detect_closing,
    detect_leave_command,
    detect_stop_command,
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

    if settings.reconcile_enabled:
        # Backstop that finalizes sessions whose Recall bot is terminal but whose
        # status webhook never arrived — keeps the per-minute meter from leaking.
        asyncio.create_task(_reconcile_sessions_loop())

    if settings.elevenlabs_api_key:
        # Pre-synthesize the fixed conversational furniture so the FIRST
        # ack/backchannel/goodbye of a meeting comes from cache, not a
        # vendor round-trip. Fire-and-forget; never delays boot.
        asyncio.create_task(_prewarm_tts_cache())

    try:
        yield
    finally:
        global _shutting_down
        _shutting_down = True
        print("[gmail-watch] shutdown signal — watcher draining", flush=True)


app = FastAPI(title="Callable AI Process Avatar", lifespan=_lifespan)
app.include_router(tts.router)  # POST /tts (open-source avatar voice)
app.include_router(org_api.router)  # /org/* — org-memory seam for surfaces (#48)

# Meeting-bound GPU runtime re-checks the live session count before it stops
# the photoreal box (a new meeting may have started during the grace window).
gpu_runtime.configure(lambda: len(store.all_sessions()))

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
REPO_ROOT_DIR = Path(__file__).resolve().parents[2]
GOOGLE_CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    # Read Laura's inbox so "Add people" invites (which email her a Meet link,
    # with no calendar event) can auto-join the meeting. See gmail_watcher.py.
    "https://www.googleapis.com/auth/gmail.readonly",
    # Read shared Drive folders so an avatar with drive_folder_id walks into
    # meetings knowing the team's docs. See drive_client.py. Adding a scope
    # means reconnecting once via /oauth/google/connect.
    "https://www.googleapis.com/auth/drive.readonly",
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
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


def _split_emails(raw: str) -> set[str]:
    return {e.lower() for e in EMAIL_RE.findall(raw or "")}


def _calendar_target_emails() -> set[str]:
    return _split_emails(settings.calendar_invite_emails)


def _google_redirect_uri() -> str:
    return settings.google_calendar_redirect_uri.strip() or (
        f"{settings.public_base_url.rstrip('/')}/oauth/google/callback"
    )


def _prebuild_indexes() -> None:
    """Build each avatar's RAG index on boot so the demo works with no setup.

    Free & instant with the default hash embedder; skipped if already current.
    """
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
_gmail_state = {"last_poll": 0.0, "last_error": "", "joined": []}
# Set when the instance is being drained (deploy/rollout). The Gmail watcher stops
# dispatching bots the moment this flips, so a draining OLD instance never races the
# NEW instance to put a second bot in the same meeting during a deploy overlap.
_shutting_down = False

# Spoken the instant she decides to web-search, so the ~4s search isn't dead air.
_SEARCH_FILLERS = (
    "Sure, let me look that up — one sec.",
    "Let me quickly check the web on that — one moment.",
    "Good one — give me a sec to search that.",
)
_SEARCH_FILLERS_IT = (
    "Certo, lo cerco subito — un attimo.",
    "Do un'occhiata veloce sul web — un momento.",
    "Bella domanda — un secondo che cerco.",
)

# Spoken when a NON-search answer is taking a beat (tool round-trips, slow
# provider) — an acknowledgment beats dead air on the interactive avatar.
_ACK_FILLERS = (
    "Give me a second to think about that.",
    "One sec — let me work that out.",
    "Hmm, give me a moment on that one.",
)
_ACK_FILLERS_IT = (
    "Dammi un secondo per pensarci.",
    "Un attimo — ci ragiono.",
    "Mmm, dammi un momento su questa.",
)

# How long a /live/act answer may take before she speaks an acknowledgment
# filler. Below this, a filler is just noise in front of an instant answer.
_ACK_FILLER_AFTER_S = 1.2

_MEET_CODE_RE = re.compile(r"meet\.google\.com/([a-z-]+)")
_BOT_TERMINAL = {"call_ended", "done", "fatal"}
# bot_ids whose finalize is currently in flight. Recall emits bot.call_ended
# THEN bot.done (both terminal) as SEPARATE concurrent webhook POSTs, and the
# reconciliation loop can race them — this in-flight set makes _finalize_session
# safe to call from all three sources without double-delivering session.ended.
_finalizing: set[str] = set()
_BOT_VARIANT_RANK = {
    "web_gpu": 0,
    "web_4_core": 1,
    "web": 3,
}
_LIVE_REPAIR_RE = re.compile(
    r"\b(can you hear|do you hear|hear me|are you there|hello|hi laura|"
    r"doesn'?t work|not working|is broken|no response|answer me|"
    r"what can you do|help me)\b",
    re.IGNORECASE,
)


def _meeting_code(url: str) -> str:
    m = _MEET_CODE_RE.search(url or "")
    return m.group(1) if m else (url or "")


def _recall_list_headers() -> dict[str, str]:
    return {
        "Authorization": settings.recall_api_key.strip(),
        "Content-Type": "application/json",
    }


def _bot_variant_rank(bot: dict) -> int:
    """Lower is better: GPU, then 4-core, then unknown paid variants, then default."""
    variant = bot.get("variant") or {}
    if isinstance(variant, dict):
        values = [str(v) for v in variant.values() if v]
    else:
        values = [str(variant)] if variant else []
    if not values:
        return _BOT_VARIANT_RANK["web"]
    return min(_BOT_VARIANT_RANK.get(v, 2) for v in values)


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
    code = _meeting_code(meeting_url)
    if not code:
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
            mu = bot.get("meeting_url")
            mid = mu.get("meeting_id") if isinstance(mu, dict) else mu
            if mid and code in str(mid):
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
    Recall itself (the source of truth) before dispatching another bot.
    """
    code = _meeting_code(meeting_url)
    if not code:
        return False
    try:
        r = httpx.get(
            f"{settings.recall_api_base.rstrip('/')}/api/v1/bot/",
            headers=_recall_list_headers(),
            timeout=20.0,
        )
        r.raise_for_status()
        for bot in (r.json().get("results") or [])[:25]:
            mu = bot.get("meeting_url")
            mid = mu.get("meeting_id") if isinstance(mu, dict) else mu
            if mid and code in str(mid):
                status = (bot.get("status_changes") or [{}])[-1].get("code")
                if status not in _BOT_TERMINAL:
                    return True
    except Exception:
        pass
    return False


async def _gmail_watch_loop() -> None:
    seeded = False  # first pass only records existing mail; never joins old meetings
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
                continue
            for _mid, url, invite_addrs in new:
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


def _bot_status_code(bot: dict) -> str | None:
    """Latest Recall status_changes code (done/call_ended/in_call_recording/…)."""
    return (bot.get("status_changes") or [{}])[-1].get("code")


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
        store.remove(bot_id)
    finally:
        _finalizing.discard(bot_id)


async def _reconcile_once() -> None:
    """One reconciliation pass: finalize every active session whose Recall bot is
    terminal, and drop orphaned sessions whose bot Recall no longer knows about.
    Extracted from the loop so it is unit-testable without touching asyncio.sleep.
    Best-effort per session — a bad poll skips that bot, never the whole pass."""
    active = store.all_sessions()  # a COPY — safe while finalize removes
    # Prune miss-counters for bots that already left the store by ANY path
    # (finalized via webhook, cancelled) so the dict can't grow dead entries.
    live_ids = {s.bot_id for s in active}
    for gone in [b for b in _reconcile_missing if b not in live_ids]:
        _reconcile_missing.pop(gone, None)
    for session in active:
        if _shutting_down:
            break
        bid = session.bot_id
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
            code = _bot_status_code(r.json())
            if code in _BOT_TERMINAL:
                # notify_failed fires ONCE, inside the guarded finalize body, so a
                # fatal seen by both this poll and the webhook notifies Cedric once.
                await _finalize_session(bid, source="reconcile", failed_code=code)
        except Exception:
            continue  # best-effort: a bad poll must never break the loop


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


@app.get("/gmail/status")
def gmail_status() -> JSONResponse:
    """Non-secret health of the Gmail 'Add people' auto-join watcher."""
    has_rt = bool(gmail_watcher.refresh_token())
    last = _gmail_state["last_poll"]
    return JSONResponse(
        {
            "enabled": settings.gmail_watch_enabled,
            "has_refresh_token": has_rt,
            "poll_seconds": settings.gmail_poll_seconds,
            "seconds_since_last_poll": round(time.time() - last, 1) if last else None,
            "last_error": _gmail_state["last_error"],
            "recent_joins": _gmail_state["joined"][-5:],
        }
    )


# ───────────────────────────── health ──────────────────────────────
@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "active_sessions": len(store.all_sessions()),
        "brain_provider": effective_provider(),
        "brain_model": settings.brain_model,
        "embedding_provider": settings.embedding_provider,
        "avatars": avatars.list_ids(),
    }


@app.get("/avatars")
def list_avatars() -> dict:
    """List installed avatars (one folder each under avatars/)."""
    out = []
    for aid in avatars.list_ids():
        a = avatars.load(aid)
        out.append({"id": a.id, "name": a.name, "role": a.role,
                    "wake_words": a.wake_words})
    return {"avatars": out}


# ─────────────────────────── demo console ──────────────────────────
# Everything below runs WITHOUT the live-meeting vendors (Recall/Anam/
# ElevenLabs). It exercises the brain + RAG directly so you can prove the value
# with zero keys (BRAIN_PROVIDER=stub) or one key (BRAIN_PROVIDER=anthropic).
@app.get("/")
def demo_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "demo.html")


class AskRequest(BaseModel):
    question: str
    avatar_id: str = "laura"


@app.post("/demo/ask")
async def demo_ask(req: AskRequest) -> JSONResponse:
    """Ask an avatar a question → grounded, cited answer (no meeting needed)."""
    avatar = avatars.load(req.avatar_id)  # raises if unknown
    result = await run_in_threadpool(answer_question, avatar, req.question)
    return JSONResponse(result)


@app.post("/live/ask")
async def live_ask(req: AskRequest) -> StreamingResponse:
    """Stream a grounded answer as SSE for the DIRECT 'talk to Laura' web avatar.

    In that mode Anam captures the user's mic + does STT/TTS/lip-sync, and calls
    THIS endpoint as its brain. Reuses the RAG + Groq streaming path — each
    grounded sentence is emitted as `data: {"content": "..."}` (and `[DONE]` at
    the end). Stays silent (no content, just [DONE]) when the SKIP gate fires.
    """
    avatar = avatars.load(req.avatar_id)

    async def gen():
        async for sentence in iterate_in_threadpool(
            answer_question_stream(avatar, req.question)
        ):
            yield f"data: {json.dumps({'content': sentence})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/live/act")
async def live_act(req: AskRequest) -> StreamingResponse:
    """Grounded answer that can ACT — the model may call tools (calculate, check a
    deadline, look up a record) before answering. Same SSE shape as /live/ask so
    the avatar page is unchanged: the final spoken answer is emitted as
    `data: {"content": "..."}` then `[DONE]`. Which tools ran is surfaced as an SSE
    comment line (`: tools_used ...`) for transparency — clients ignore it.

    Kept OFF the streaming meeting hot path on purpose: tool use needs a round-trip
    first, so this is for the direct web avatar / demo.
    """
    avatar = avatars.load(req.avatar_id)

    async def gen():
        # Start the answer immediately, then never leave dead air while it cooks:
        # a web search gets its filler up front (it's a ~4s+ break by definition);
        # any other answer that takes more than a beat gets a spoken
        # acknowledgment so she visibly "took the question" instead of freezing.
        task = asyncio.ensure_future(
            run_in_threadpool(answer_with_tools, avatar, req.question)
        )
        if wants_web_search(req.question):
            filler = _line_for(req.question, list(_SEARCH_FILLERS), list(_SEARCH_FILLERS_IT))
            yield f"data: {json.dumps({'content': filler})}\n\n"
        else:
            done, _ = await asyncio.wait({task}, timeout=_ACK_FILLER_AFTER_S)
            if not done:
                filler = _line_for(req.question, list(_ACK_FILLERS), list(_ACK_FILLERS_IT))
                yield f"data: {json.dumps({'content': filler})}\n\n"
        try:
            result = await task
        except Exception as e:  # noqa: BLE001 — a failed lookup must never end in silence
            print(f"[live/act] answer failed: {e}", flush=True)
            fail = "Sorry — that one failed on me. Mind asking again?"
            yield f"data: {json.dumps({'content': fail})}\n\n"
            yield "data: [DONE]\n\n"
            return
        used = result.get("tools_used") or []
        if used:
            yield f": tools_used {', '.join(u['tool'] for u in used)}\n\n"
        answer = (result.get("answer") or "").strip()
        if answer:
            yield f"data: {json.dumps({'content': answer})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


class PostMeetingRequest(BaseModel):
    transcript: str
    avatar_id: str = "laura"


@app.post("/demo/post_meeting")
async def demo_post_meeting(req: PostMeetingRequest) -> JSONResponse:
    """Turn a meeting transcript into the full post-meeting artifact."""
    avatar = avatars.load(req.avatar_id)
    artifact = await run_in_threadpool(post_meeting, avatar, req.transcript)
    # Echo the transcript so the demo artifact matches the live one
    # (_finalize_session does the same); the page shows it in a transcript tab.
    artifact["transcript"] = req.transcript
    return JSONResponse(artifact)


@app.get("/demo/sample")
def demo_sample(avatar_id: str = "laura") -> JSONResponse:
    """A sample transcript to load into the post-meeting demo, if the avatar has one."""
    avatar = avatars.load(avatar_id)
    sample = avatar.dir / "sample_meeting.txt"
    text = sample.read_text() if sample.exists() else ""
    return JSONResponse({"avatar_id": avatar_id, "transcript": text})


# ── live avatar preview (Anam face + Claude brain, NO meeting vendor) ──
# Lets you SEE the talking avatar answer from the docs without Recall/ngrok.
@app.get("/live")
def live_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "live.html")


@app.get("/join")
def join_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "join.html")


@app.get("/talk")
def talk_page() -> FileResponse:
    """Open-source avatar page (TalkingHead + our TTS) — the Anam replacement.
    Recall will render this instead of avatar.html once it's proven out.

    no-store: the page's JS changes often (framing, barge-in, streaming) — without
    this browsers serve a stale cached copy and users see old behaviour."""
    return FileResponse(
        FRONTEND_DIR / "talk.html", headers={"Cache-Control": "no-store"}
    )


@app.get("/photoreal")
def photoreal_page() -> FileResponse:
    """Photoreal avatar page (Stage 2): GPU-streamed MuseTalk face. Same speak
    contract as /talk; flip meetings onto it with AVATAR_PAGE=photoreal once
    the GPU box is live (see gpu/README.md)."""
    return FileResponse(FRONTEND_DIR / "photoreal.html")


@app.get("/photoreal/config")
def photoreal_config() -> JSONResponse:
    """Where the photoreal page finds the GPU frame stream (empty = fallback)."""
    return JSONResponse(
        {"stream_url": settings.gpu_stream_url},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/laura-reference.jpg")
def photoreal_reference() -> FileResponse:
    """Static reference portrait — the photoreal page's no-GPU fallback face."""
    return FileResponse(
        REPO_ROOT_DIR / "gpu" / "assets" / "reference.jpg",
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.api_route("/{avatar_id}.glb", methods=["GET", "HEAD"])
def talk_avatar_model(avatar_id: str) -> Response:
    """Per-avatar 3D model for /talk (laura.glb, cedric.glb, …), served
    same-origin on purpose: Ready Player Me's CDN shutdown (Jan 2026) killed our
    previous third-party model URL, so the models (TalkingHead-repo samples) are
    vendored into frontend/. /talk HEAD-probes /{avatar_id}.glb and falls back to
    /laura.glb, so a missing model 404s here without ever breaking the page.
    HEAD must be explicit — FastAPI's @app.get alone 405s it, which would have
    silently defeated the probe (curl -I caught this; FileResponse handles HEAD
    natively). Whitelisted to simple ids resolving to real files — never a
    path traversal."""
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", avatar_id):
        return JSONResponse({"error": "unknown model"}, status_code=404)
    model_path = FRONTEND_DIR / f"{avatar_id}.glb"
    if not model_path.is_file():
        return JSONResponse({"error": "unknown model"}, status_code=404)
    return FileResponse(
        model_path,
        media_type="model/gltf-binary",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# TTS for the open-source avatar page lives in its own router (tts.py) — a
# self-contained concern with no coupling to the live meeting path.


class LiveTokenRequest(BaseModel):
    avatar_id: str = "laura"


class LiveError(BaseModel):
    where: str = ""
    message: str = ""
    stack: str = ""


@app.post("/live/error")
async def live_error(e: LiveError) -> JSONResponse:
    """The avatar page reports client-side Anam errors here so we can see them."""
    print(f"\n[LIVE-ERROR] {e.where}: {e.message}\n{(e.stack or '')[:1500]}\n", flush=True)
    return JSONResponse({"ok": True})


@app.post("/live/token")
async def live_token(req: LiveTokenRequest) -> JSONResponse:
    """Mint a fresh Anam session token for the browser to stream the avatar."""
    avatar = avatars.load(req.avatar_id)
    try:
        persona_id = await run_in_threadpool(anam_client.create_persona, avatar)
        convo = await run_in_threadpool(
            anam_client.create_conversation, avatar, persona_id
        )
    except Exception as e:  # surface a clean message to the page
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(
        {"avatar_id": avatar.id, "session_token": convo["conversation_url"]}
    )


# ── Recall diagnostics: non-secret readiness/auth check for live meetings ──
@app.get("/recall/status")
def recall_status(check_auth: bool = False) -> JSONResponse:
    """Report Recall setup state without returning any secret values."""
    status = recall_client.auth_check() if check_auth else recall_client.readiness()
    return JSONResponse(status, status_code=200 if status["ready"] else 400)


# ── Google Calendar OAuth: connect Laura's calendar to Recall Calendar V2 ──
@app.get("/oauth/google/connect")
def google_oauth_connect():
    """Start Google OAuth for the calendar account that should invite Laura."""
    if not settings.google_calendar_client_id:
        return JSONResponse(
            {"error": "GOOGLE_CALENDAR_CLIENT_ID is not set."}, status_code=400
        )

    params = {
        "client_id": settings.google_calendar_client_id,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(GOOGLE_CALENDAR_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if settings.calendar_oauth_state:
        params["state"] = settings.calendar_oauth_state

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return RedirectResponse(url)


@app.get("/oauth/google/callback")
async def google_oauth_callback(
    code: str = "", state: str = "", error: str = ""
) -> JSONResponse:
    """Finish Google OAuth, then create the Recall calendar connection."""
    if error:
        return JSONResponse({"error": error}, status_code=400)
    if not code:
        return JSONResponse({"error": "Missing Google OAuth code."}, status_code=400)
    if settings.calendar_oauth_state and state != settings.calendar_oauth_state:
        return JSONResponse({"error": "Invalid OAuth state."}, status_code=400)
    if not settings.google_calendar_client_id:
        return JSONResponse(
            {"error": "GOOGLE_CALENDAR_CLIENT_ID is not set."}, status_code=400
        )
    if not settings.google_calendar_client_secret:
        return JSONResponse(
            {"error": "GOOGLE_CALENDAR_CLIENT_SECRET is not set."}, status_code=400
        )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_calendar_client_id,
                    "client_secret": settings.google_calendar_client_secret,
                    "redirect_uri": _google_redirect_uri(),
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            token = token_resp.json()

            refresh_token = token.get("refresh_token", "")
            if not refresh_token:
                return JSONResponse(
                    {
                        "error": (
                            "Google did not return a refresh_token. Re-open "
                            "/oauth/google/connect and approve with prompt=consent; "
                            "if needed, revoke the app in Google settings first."
                        )
                    },
                    status_code=400,
                )

            oauth_email = ""
            access_token = token.get("access_token", "")
            if access_token:
                profile_resp = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if 200 <= profile_resp.status_code < 300:
                    oauth_email = profile_resp.json().get("email", "").lower()
    except httpx.HTTPStatusError as e:
        return JSONResponse(
            {"error": "Google OAuth token exchange failed.", "status": e.response.status_code},
            status_code=400,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    targets = _calendar_target_emails()
    if targets and oauth_email not in targets:
        return JSONResponse(
            {
                "error": "Wrong Google account connected.",
                "connected_email": oauth_email or None,
                "expected_email": sorted(targets),
            },
            status_code=400,
        )

    try:
        calendar = await run_in_threadpool(
            lambda: recall_client.create_calendar(
                oauth_client_id=settings.google_calendar_client_id,
                oauth_client_secret=settings.google_calendar_client_secret,
                oauth_refresh_token=refresh_token,
                oauth_email=oauth_email,
                metadata={
                    "avatar_id": "laura",
                    "invite_filter": ",".join(sorted(targets)),
                },
            )
        )
    except Exception as e:
        return JSONResponse({"error": f"Recall calendar creation failed: {e}"}, status_code=400)

    return JSONResponse(
        {
            "ok": True,
            "calendar_id": calendar.get("id"),
            "calendar_status": calendar.get("status"),
            "connected_email": oauth_email or None,
            "invite_filter": sorted(targets),
            "webhook_url": f"{settings.public_base_url.rstrip('/')}/webhooks/recall-calendar",
        }
    )


# ── Granola: pull a real finished transcript (post-meeting only) ──
@app.get("/granola/notes")
def granola_notes(limit: int = 20) -> JSONResponse:
    """List recent Granola notes to pick from (needs GRANOLA_API_KEY)."""
    if not settings.granola_api_key:
        return JSONResponse({"error": "GRANOLA_API_KEY not set"}, status_code=400)
    return JSONResponse({"notes": granola_client.list_notes(limit)})


@app.get("/granola/transcript")
def granola_transcript(note_id: str) -> JSONResponse:
    """Fetch one Granola note's transcript as 'Speaker: text' lines."""
    if not settings.granola_api_key:
        return JSONResponse({"error": "GRANOLA_API_KEY not set"}, status_code=400)
    return JSONResponse({"note_id": note_id,
                         "transcript": granola_client.get_transcript(note_id)})


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


async def _start_avatar_session(
    meeting_url: str,
    avatar_id: str = "",
    join_at: Optional[str] = None,
    integration: Optional[dict] = None,
) -> dict:
    """Send a Recall bot (rendering the avatar page as its camera) into a meeting.

    Shared by the manual /sessions/start endpoint, the calendar auto-join webhook,
    and the Gmail watcher. Raises on failure. The avatar page mints its own fresh
    Anam token at render time and keys its websocket on the conversation_id.
    """
    avatar = avatars.load(avatar_id or settings.default_avatar_id)  # raises if unknown
    conversation_id = uuid.uuid4().hex
    avatar_url = (
        f"{settings.public_base_url.rstrip('/')}/{settings.avatar_page.strip('/')}"
        f"?avatar_id={avatar.id}&conversation_id={conversation_id}"
        f"&body={avatar.talk_body}"
    )
    bot = await run_in_threadpool(
        recall_client.create_bot, meeting_url, avatar_url, join_at, avatar.name
    )
    session = store.create(
        bot_id=bot["id"], meeting_url=meeting_url, avatar_id=avatar.id
    )
    # CEDRIC: a summon that didn't carry its own wiring (the Gmail auto-join
    # watcher passes integration=None) still gets the Model A default routing —
    # otherwise an email-summoned meeting silently falls to Model B (no context
    # pull, no session.ended to Cedric). POST /sessions/start always passes its
    # own build_integration result, so it keeps winning; default_integration()
    # returns None when no SURFACE_* is set, leaving plain deployments unchanged.
    if integration is None:
        integration = cedric.default_integration()
    if integration:
        session.integration = integration
    session.anam_conversation_id = conversation_id
    store.register_conversation(conversation_id, bot["id"])
    # Cross-meeting memory: what previous sessions of this meeting link left
    # open. One sqlite read at start; "" when the meeting has no history.
    session.memory_brief = await run_in_threadpool(
        ledger.carryover_brief, meeting_url
    )
    # Drive connector: the avatar's shared folder (avatar.yaml drive_folder_id)
    # becomes part of the same brief channel. Fetched HERE — session start,
    # off the live path, cached in drive_client — and best-effort: any failure
    # returns "" and the join proceeds without it.
    if avatar.drive_folder_id:
        folder = await run_in_threadpool(
            drive_client.folder_brief, avatar.drive_folder_id
        )
        if folder:
            session.memory_brief = (
                f"[Shared Drive folder — current team docs]\n{folder}\n\n"
                + (session.memory_brief or "")
            )
    if settings.autopilot_brief and session.memory_brief:
        # Autopilot: mail/Slack "what's still open from last time" to the
        # owner as the bot joins. Fire-and-forget — never delays the join.
        asyncio.create_task(
            run_in_threadpool(autopilot.maybe_send_brief, meeting_url, avatar.name)
        )
    # Photoreal only: wake the GPU box for this meeting (fire-and-forget; the
    # page runs on the static-portrait fallback until the stream comes up).
    gpu_runtime.on_session_started()
    return {
        "bot_id": bot["id"],
        "conversation_id": conversation_id,
        "avatar_page_url": avatar_url,
        "scheduled_for": join_at,
    }


@app.post("/sessions/start")
async def start_session(req: StartRequest, request: Request) -> JSONResponse:
    if err := cedric.auth_error(request):  # CEDRIC
        return err
    try:
        recall_client.assert_ready()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    brief = req.context.brief_markdown if req.context else ""  # CEDRIC
    if err := cedric.brief_too_large(brief):  # CEDRIC
        return err
    # One live/scheduled booking per meeting URL: rebooking must cancel first
    # (otherwise two bots — and two per-minute meters — end up in one call).
    for existing in store.all_sessions():
        if existing.meeting_url == req.meeting_url:
            return JSONResponse(
                {
                    "error": "a session already exists for this meeting_url",
                    "bot_id": existing.bot_id,
                },
                status_code=409,
            )

    integration = cedric.build_integration(req, brief)  # CEDRIC
    try:
        result = await _start_avatar_session(
            req.meeting_url, req.avatar_id, req.join_at, integration
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(result)


def _norm_action_text(text: str) -> str:
    """Normalization for action-item dedupe (mirrors ledger._norm's intent)."""
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def _merge_action_items(queued: list, extracted: list) -> list:
    """Artifact actions[] = live-captured queue_action items first, then the
    summarizer's extraction, deduped on normalized item text. A live capture
    wins a collision — it is the wording the room actually asked for — and
    ledger.record_meeting dedupes again on insert, so double-merging is safe.

    Every returned action carries a stable ``action_id``: a live capture keeps
    the id assigned at capture time (the same one already sent on its
    action.requested webhook), and a summarizer-only action — which never fired
    a live event — gets a fresh id here. The id is what the orchestrator dedupes
    and resolves on."""
    merged: list = []
    seen: set[str] = set()
    for q in queued or []:
        text = (q.get("action") or "").strip()
        key = _norm_action_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        owner = (q.get("owner") or "").strip()
        merged.append(
            {
                "action_id": q.get("action_id") or ledger.new_action_id(),
                "item": text,
                "owner": owner or "UNASSIGNED",
                "deadline": (q.get("due") or "").strip(),
                "gap_type": "none" if owner else "owner",
                "requested_live": True,  # additive marker: asked out loud in-meeting
            }
        )
    for a in extracted or []:
        text = a.get("item", "") if isinstance(a, dict) else str(a)
        key = _norm_action_text(text)
        if key in seen:
            continue
        if key:
            seen.add(key)
        a = dict(a) if isinstance(a, dict) else {"item": text}
        a.setdefault("action_id", ledger.new_action_id())
        merged.append(a)
    return merged


async def _finalize_session(
    bot_id: str, source: str = "manual", failed_code: str = ""
) -> dict | None:
    """End a session once: stop both vendors, build + store the artifact.

    Idempotent AND concurrency-safe — safe to call from the manual endpoint, the
    terminal-status webhook, and the reconciliation loop, even simultaneously.
    Returns the stored artifact (or None) if the session is already gone.

    ``source`` (manual/webhook/reconcile) is recorded PII-safely for diagnosing
    which path finalized a meeting; it never affects behaviour. ``failed_code``
    ('fatal' when the join fatally failed) fires the Cedric join-failed
    notification ONCE, inside the guard — so a fatal seen by both the webhook and
    the poll notifies the orchestrator a single time, not twice.
    """
    session = store.get(bot_id)
    if session is None:
        return store.get_artifact(bot_id)
    # Concurrency guard. store.remove(bot_id) — the thing that makes the
    # `session is None` check above idempotent — only runs at the very END of
    # the body, past several awaits (the multi-second post_meeting LLM call
    # included). So two finalizes racing on the same bot (bot.call_ended +
    # bot.done arrive as concurrent webhook POSTs; the poll loop is a third
    # racer) would BOTH pass the None check and BOTH fire session.ended to
    # Cedric. Check-and-add is synchronous — no await between here and the add —
    # so it is atomic under asyncio's single-threaded loop.
    if bot_id in _finalizing:
        return store.get_artifact(bot_id)
    _finalizing.add(bot_id)
    try:
        return await _finalize_session_locked(bot_id, session, source, failed_code)
    finally:
        _finalizing.discard(bot_id)


async def _finalize_session_locked(
    bot_id: str, session: store.Session, source: str, failed_code: str = ""
) -> dict | None:
    """The actual finalize body, run under the ``_finalizing`` in-flight guard."""
    # Join-failed notification (fatal): fire here, under the guard, so it runs at
    # most once per bot even when the webhook and the poll both observe the fatal.
    if failed_code == "fatal":
        cedric.notify_failed(session, bot_id, failed_code)  # CEDRIC
    transcript_text = session.transcript_text()

    # Stop billing on both vendors.
    await run_in_threadpool(recall_client.leave_call, bot_id)
    if session.anam_conversation_id:
        await run_in_threadpool(
            anam_client.end_conversation, session.anam_conversation_id
        )

    artifact: dict = {
        "summary": "",
        "decisions": [],
        "actions": [],
        "checklist": [],
        "missing_steps": [],
        "readiness_score": 0,
        "risks": [],
        "follow_up_email": {},
    }
    integration = dict(session.integration) if session.integration else None  # CEDRIC
    if transcript_text.strip():
        avatar = avatars.load(session.avatar_id)
        artifact = await run_in_threadpool(
            lambda: post_meeting(
                avatar,
                transcript_text,
                context=(integration or {}).get("brief", ""),  # CEDRIC: brief-grounded summary
            )
        )

    # Live-captured action requests (tools.queue_action): fold them into the
    # artifact's actions[] ahead of the summarizer's extraction, deduped on
    # normalized item text. Runs BEFORE save_artifact and ledger.record_meeting
    # so every consumer — stored artifact, wire artifact, ledger, autopilot —
    # sees the same merged list. Plain non-orchestrated sessions benefit too.
    # Always run the merge (even with no live captures) so EVERY action carries
    # a stable action_id — the summarizer-only actions get one here too.
    queued_actions = list(getattr(session, "queued_actions", None) or [])
    artifact["actions"] = _merge_action_items(
        queued_actions, artifact.get("actions") or []
    )
    artifact["checklist"] = artifact["actions"]  # legacy alias, same list

    # The transcript is the raw material of the artifact — persist it so the
    # product output is complete (transcript + summary + checklist + email).
    # It lives only in the artifact store (PII: never logged).
    artifact["transcript"] = transcript_text

    store.save_artifact(bot_id, artifact)
    # Cross-meeting memory: fold this meeting's extracted facts into the
    # ledger. Best-effort — memory must never block the cleanup below
    # (session removal + GPU meter signal), so a ledger hiccup is swallowed.
    try:
        await run_in_threadpool(
            ledger.record_meeting, session.meeting_url, session.avatar_id, bot_id, artifact
        )
    except Exception:
        pass
    orchestrated = cedric.deliver_ended(integration, bot_id, artifact)  # CEDRIC
    if orchestrated:
        pass  # orchestrated session: the orchestrator owns delivery, skip autopilot
    elif settings.autopilot_deliver:
        # Autopilot: send the drafted follow-up + Slack summary now, without
        # holding up the finalize response (meter is already stopped above).
        # Best-effort like the ledger — never blocks the cleanup below.
        try:
            name = avatars.load(session.avatar_id).name
            asyncio.create_task(run_in_threadpool(autopilot.maybe_deliver, name, artifact))
        except Exception:
            pass
    # PII-safe finalize telemetry: counts + booleans only, NEVER any utterance
    # text (the guard hook enforces this). Lets the next live test see which
    # path finalized and whether content actually accumulated — the question
    # left open by the empty-artifact test run.
    n_lines = len(session.transcript)
    had_content = bool(transcript_text.strip())
    print(
        f"[finalize] bot={bot_id} source={source} lines={n_lines} "
        f"orchestrated={orchestrated} content={had_content}",
        flush=True,
    )
    store.remove(bot_id)
    # Photoreal only: last session out turns off the GPU meter (after a grace
    # window, in case another meeting starts right away).
    gpu_runtime.on_session_ended(len(store.all_sessions()))
    return artifact


@app.post("/sessions/{bot_id}/end")
async def end_session(bot_id: str, request: Request) -> JSONResponse:
    if err := cedric.auth_error(request):  # CEDRIC
        return err
    artifact = await _finalize_session(bot_id, source="manual")
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
    return JSONResponse(cedric.wire_artifact(artifact))  # CEDRIC: PII stays home


@app.post("/sessions/{bot_id}/cancel")
async def cancel_session(bot_id: str, request: Request) -> JSONResponse:
    """Cancel a scheduled bot / abort a live one WITHOUT building an artifact.

    Used by the orchestrator when a calendar event moves or is cancelled (it
    rebooks afterwards). `end` keeps its meaning: finalize + artifact.
    """
    if err := cedric.auth_error(request):  # CEDRIC
        return err
    session = store.get(bot_id)
    if session is None:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    # Stop the meter: works for live bots; scheduled bots may reject leave_call,
    # so fall back to deleting the scheduled bot. Best-effort on both — the
    # session is removed either way and never produces an artifact.
    try:
        await run_in_threadpool(recall_client.leave_call, bot_id)
    except Exception:
        try:
            await run_in_threadpool(recall_client.delete_bot, bot_id)
        except Exception as e:  # noqa: BLE001 — surface but don't fail the cancel
            print(f"[sessions] cancel: recall cleanup failed: {e}", flush=True)
    if session.anam_conversation_id:
        try:
            await run_in_threadpool(
                anam_client.end_conversation, session.anam_conversation_id
            )
        except Exception:
            pass
    store.remove(bot_id)
    gpu_runtime.on_session_ended(len(store.all_sessions()))
    return JSONResponse({"cancelled": True, "bot_id": bot_id})


class DeliverRequest(BaseModel):
    to: list[str] = []
    slack: bool = True


@app.post("/sessions/{bot_id}/deliver")
async def deliver_artifact(bot_id: str, req: DeliverRequest, request: Request) -> JSONResponse:
    """Actually send the finished meeting's follow-up email + post it to Slack."""
    if err := cedric.auth_error(request):  # CEDRIC
        return err
    artifact = store.get_artifact(bot_id)
    if artifact is None:
        return JSONResponse({"error": "no artifact for this bot_id"}, status_code=404)

    avatar = avatars.load(store.get(bot_id).avatar_id) if store.get(bot_id) else None
    name = avatar.name if avatar else avatars.load(settings.default_avatar_id).name
    email = artifact.get("follow_up_email", {}) or {}

    email_res = await run_in_threadpool(
        actions.send_email, req.to, email.get("subject", ""), email.get("body", "")
    )
    slack_res = {"sent": False, "reason": "disabled"}
    if req.slack:
        slack_res = await run_in_threadpool(
            actions.post_to_slack, actions.artifact_to_slack_text(name, artifact)
        )
    return JSONResponse({"email": email_res, "slack": slack_res})


@app.get("/ledger")
def ledger_view(meeting_url: str, request: Request) -> JSONResponse:
    """Cross-meeting memory for a meeting link: every ledger item plus the
    carryover brief the avatar gets injected at the next session."""
    if err := cedric.auth_error(request):  # CEDRIC
        return err
    key = ledger.meeting_key(meeting_url)
    return JSONResponse(
        {
            "meeting_key": key,
            "brief": ledger.carryover_brief(meeting_url),
            "items": ledger.items(key),
        }
    )


@app.get("/sessions/{bot_id}/artifact")
def session_artifact(bot_id: str, request: Request) -> JSONResponse:
    """Retrieve a finished session's artifact (summary + checklist + email)."""
    if err := cedric.auth_error(request):  # CEDRIC
        return err
    live = store.get(bot_id)
    if live is not None:
        return JSONResponse({"status": "in_progress", "bot_id": bot_id})
    artifact = store.get_artifact(bot_id)
    if artifact is None:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    return JSONResponse({"status": "done", **cedric.wire_artifact(artifact)})  # CEDRIC: PII stays home


@app.get("/meetings")
def meetings_page() -> FileResponse:
    """Archive UI: every finished meeting's artifact, transcript included."""
    return FileResponse(FRONTEND_DIR / "meetings.html")


@app.get("/meetings/list")
def meetings_list() -> JSONResponse:
    """All saved artifacts, newest first, for the /meetings page. Transcripts
    are PII: they may be *served* (that's the product) but never logged."""
    return JSONResponse({"meetings": store.list_artifacts()})


# ───────────────────────── avatar page + ws ─────────────────────────
@app.get("/avatar")
def avatar_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "avatar.html")


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
        lines = session.transcript[session.summary_upto : cutoff]
        if not lines:
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

# Confirmation for a captured action request (queue_action seam): promises
# follow-up after the call, never execution. Fixed lines so they're TTS-
# prewarmed — the confirmation must land as fast as an ack.
_QUEUE_LINES = [
    "Got it — I'll queue that for approval in Slack right after the call.",
    "Noted — I'll line that up for approval in Slack once we wrap.",
    "On it — it goes to Slack for approval right after this meeting.",
]
_QUEUE_LINES_IT = [
    "Ricevuto — lo metto in coda su Slack per l'approvazione appena finiamo.",
    "Segnato — parte su Slack per l'approvazione subito dopo la call.",
]

# Listening cues spoken WHILE a human is mid-monologue (backchanneling, the
# thing that makes a listener feel present). Two syllables max — anything
# longer becomes an interruption instead of a nod.
_BACKCHANNEL_LINES = ["Mm-hm.", "Mm.", "Right."]
_BACKCHANNEL_LINES_IT = ["Mm-hm.", "Mm.", "Capito."]


def _avatar_voice(session: "store.Session") -> str:
    """The session avatar's ElevenLabs voice for TTS. avatars.load is
    mtime-cached (hot-path safe); any failure falls back to the global voice
    ("" keeps the shared prewarm cache key)."""
    try:
        return avatars.load(session.avatar_id).elevenlabs_voice_id or ""
    except Exception:  # noqa: BLE001
        return ""


def _line_for(heard: str, en: list, it: list) -> str:
    """A random line from the pool matching the language of what was heard."""
    return random.choice(it if sounds_italian(heard) else en)


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


def _is_own_speech(avatar_name: str, speaker: str) -> bool:
    """True when a transcript line is the avatar's OWN voice — the meeting bot
    hears the avatar too. The Recall bot's display name is the avatar's name
    (recall_client.create_bot(bot_name=avatar.name)), so one comparison covers
    both; no hardcoded persona name, so a human participant who shares a name
    with a DIFFERENT avatar is never silenced."""
    return speaker.strip().lower() == avatar_name.strip().lower()


def _in_opening_grace(session: store.Session) -> bool:
    """Opening settle-in ("wait to be called"): True while she should stay silent
    unless DIRECTLY addressed by name. Ends the instant she's first addressed
    (session.addressed_once) or after settings.opening_grace_seconds from join,
    whichever comes first — so she never talks over the room while it settles,
    but engages immediately when named and becomes proactive once things settle
    even if nobody names her."""
    if settings.opening_grace_seconds <= 0 or session.addressed_once:
        return False
    return (time.time() - session.created_at) < settings.opening_grace_seconds


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
    return any(
        now - ts < 45.0 and norm in spoken
        for spoken, ts in session._recent_lines.items()
    )


# Partials made ONLY of filler/backchannel tokens ("yeah yeah", "uh uh ok",
# "sì sì va bene") — listening noises, never an interruption.
_FILLER_ONLY = re.compile(
    r"^(?:\s*(?:uh|um|mm+|hm+|eh|ah|oh|yeah|yep|yes|no|ok(?:ay)?|right|sure|"
    r"sì|si|già|va bene|bene|certo|ecco|beh|cioè|esatto|capito|giusto)"
    r"\b[\s,.!?]*)+$",
    re.IGNORECASE,
)


def _should_barge_in(session: store.Session, avatar_name: str, speaker: str, text: str) -> bool:
    """A human talked while Laura is (estimated) still speaking -> interrupt her.

    Not her own transcribed speech (the meeting bot hears her too), not her own
    ECHO through someone's open mic, and not filler/backchannels ("yeah yeah",
    "ok right") — those shouldn't cut her off.
    """
    if not settings.barge_in_enabled:
        return False
    if _is_own_speech(avatar_name, speaker):
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
                f"{settings.public_base_url.rstrip('/')}/{settings.avatar_page.strip('/')}"
                f"?avatar_id={avatar.id}&conversation_id={conversation_id}"
                f"&body={avatar.talk_body}"
            )
            bot = await run_in_threadpool(
                recall_client.create_bot, url, avatar_url, start, avatar.name
            )
            s = store.create(bot_id=bot["id"], meeting_url=url, avatar_id=avatar.id)
            # CEDRIC: calendar-summoned (scheduled) bots take this inlined path,
            # NOT _start_avatar_session, so wire the Model A default here too —
            # otherwise a calendar invite bypasses the orchestrator exactly like
            # the email path did. None when no SURFACE_* is set.
            default_integ = cedric.default_integration()
            if default_integ:
                s.integration = default_integ
            s.anam_conversation_id = conversation_id
            store.register_conversation(conversation_id, bot["id"])
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


# ───────────────────────── recall webhook ──────────────────────────
@app.post("/webhooks/recall")
async def recall_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    # Realtime transcript webhooks (from the bot's realtime_endpoints) arrive
    # UNSIGNED — unlike the Svix-signed calendar/dashboard webhooks. If we required
    # a signature we'd 401 every transcript and the avatar would never hear its
    # wake word. So verify only when a signature is actually present (still reject
    # a bad one); accept unsigned realtime transcripts.
    has_signature = any(
        h in request.headers for h in ("webhook-signature", "svix-signature")
    )
    if has_signature:
        try:
            recall_client.verify_webhook(raw_body, request.headers)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=401)

    payload = json.loads(raw_body or b"{}")
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
        words = data.get("words", [])
        text = " ".join(w.get("text", "") for w in words).strip()
        participant = data.get("participant") or {}
        speaker = session.resolve_speaker(participant.get("name"), participant.get("id"))
        if not text:
            return JSONResponse({"ok": True})
        avatar = avatars.load(session.avatar_id)
        if _should_barge_in(session, avatar.name, speaker, text):
            await _make_avatar_stop(session)
        if _is_own_speech(avatar.name, speaker):
            return JSONResponse({"ok": True, "partial": True})
        # Her own echo through an open mic is not a human talking: it must not
        # ack, backchannel, or (via the stamp below) cancel a deference wait.
        if _is_echo(session, text):
            return JSONResponse({"ok": True, "partial": True, "echo": True})
        # A human is audibly talking right now — any deference window waiting
        # on the final-transcript path sees this and yields to them.
        session.last_human_partial_at = time.time()
        called, question = detect_wake(avatar, text, session.present_names())
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
            and detect_wake(avatar, text, session.present_names(), fuzzy=False)[0]
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
        if not called and not acked and _should_backchannel(session, text):
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
            label = session.resolve_speaker(p.get("name"), p.get("id"))
            avatar = avatars.load(session.avatar_id)
            if not _is_own_speech(avatar.name, label):  # the bot joins too
                key = str(p.get("id")) if p.get("id") is not None else label
                is_new = key not in session.participants
                session.participant_event(
                    p.get("name"), p.get("id"),
                    here=(event == "participant_events.join"),
                )
                # ── footing: greet a late joiner by name ──
                # Only when the meeting is genuinely underway (start-of-call
                # joins greet each other anyway), only for NEW named humans,
                # and never over her own voice. Cheap acknowledgment has an
                # outsized social payoff (research doc).
                if (
                    event == "participant_events.join"
                    and settings.greet_joiners
                    and is_new
                    and len(session.transcript) >= 4
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
                bid, source="webhook", failed_code="fatal" if failed else ""
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

    words = data.get("words", [])
    text = " ".join(w.get("text", "") for w in words).strip()
    participant = data.get("participant") or {}
    speaker = session.resolve_speaker(participant.get("name"), participant.get("id"))
    if not text:
        return JSONResponse({"ok": True})

    # Her own voice re-entering through a participant's open mic: not a human
    # line. Keep it out of the transcript (it would pollute per-person
    # tracking and could even get ANSWERED as if a person said it).
    if _is_echo(session, text):
        return JSONResponse({"ok": True, "spoke": False, "reason": "echo"})

    session.add_utterance(speaker, text)
    avatar = avatars.load(session.avatar_id)

    # ── barge-in: never talk over a human ──
    # If someone starts speaking while Laura is still talking, stop her mouth
    # first (the page cancels TTS instantly), then process what they said.
    if _should_barge_in(session, avatar.name, speaker, text):
        await _make_avatar_stop(session)

    # ── silent intelligence layer ──
    # Fold this line into the structured MeetingState (steps covered, decisions,
    # owners, deadlines, risks) BEFORE any speak decision. Pure regex — adds no
    # latency to the live path — and informs both the closing intervention below
    # and the post-meeting artifact.
    state = meeting_state.observe(session, avatar, speaker, text)

    # ── rolling meeting notes (background) ──
    # Every ~20 lines, fold the transcript older than the live history window
    # into short running notes (fast model, off the hot path) so her context
    # is the WHOLE meeting, not just the last 8 lines.
    _maybe_refresh_rolling_summary(session, avatar)

    # ── never converse with yourself ──
    # The bot transcribes Laura's own speech too. Answering it creates greeting
    # loops ("I'm doing well…" -> hears it -> replies -> …). Her lines stay in
    # the transcript and state above, but never reach the speak gates below.
    if _is_own_speech(avatar.name, speaker):
        return JSONResponse({"ok": True, "spoke": False, "reason": "own speech"})

    # Cross-meeting memory: lazily (re)load after a process restart.
    if session.memory_brief is None:
        session.memory_brief = await run_in_threadpool(
            ledger.carryover_brief, session.meeting_url
        )
    memory = session.memory_brief or ""
    memory = cedric.inject_brief(session, memory)  # CEDRIC: brief ahead of carryover

    # ── proactive intervention (fires once, as the meeting wraps up) ──
    if (
        settings.proactive_enabled
        and not session.proactive_done
        and detect_closing(text)
        and not session.in_cooldown(avatar.speak_cooldown_seconds)
    ):
        flag = await run_in_threadpool(
            proactive_flag, avatar, session.transcript_text(), state=state, memory=memory
        )
        conf = float(flag.get("confidence", 0.0))
        if flag.get("should_speak") and flag.get("line") and conf >= settings.proactive_min_confidence:
            session.proactive_done = True
            cits = flag.get("citations", [])
            line = flag["line"] + (f" — per {cits[0]}" if cits else "")
            await _make_avatar_speak(session, line, cits)
            return JSONResponse({"ok": True, "spoke": True, "proactive": True, "line": line})

    # ── when-to-speak gate ──
    # By default (require_wake_word=False) she answers any grounded question; the
    # SKIP sentinel + cooldown keep her from interjecting on things she can't ground.
    called, question = detect_wake(avatar, text, session.present_names())

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

    # ── action-capture continuation ──
    # A same-speaker follow-up right after a captured action (and NOT a new
    # wake) extends the captured item's text, so the artifact/ledger get the
    # whole ask even when ASR split it across finals. Short window only.
    pending = getattr(session, "last_capture", None)
    if pending is not None:
        p_item, p_speaker, p_ts = pending
        if not called and speaker == p_speaker and time.time() - p_ts < 4.0:
            p_item["action"] = " ".join((p_item["action"] + " " + text).split())[:300]
            session.last_capture = (p_item, p_speaker, time.time())
            return JSONResponse({"ok": True, "spoke": False, "capture_extended": True})
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
    # finals — the first wakes (bare name, empty question), the second isn't a
    # wake, so neither final alone fires the dismissal and a background reconcile
    # poll ends the meeting late. Complete it: if the SAME speaker addressed the
    # avatar with a BARE name in the last few seconds, re-check the leave command
    # on the concatenated finals.
    #
    # Meter safety (an early leave kills a live paid meeting) is why the split
    # path is deliberately narrow. It fires ONLY when: (1) the address turn was
    # bare — a substantive "Cedric, can you check the budget" never arms it, so a
    # later same-speaker aside can't end the call — AND (2) the follow-up doesn't
    # name another KNOWN participant (a dismissal like "Sara, you can leave" is
    # aimed at Sara, not the avatar). detect_leave_command's own guards apply on
    # top.
    #
    # Accepted narrow residual: a bare "Cedric." followed within 4s by a
    # same-speaker dismissal aimed at someone the roster doesn't yet know (a
    # never-spoken participant) or at no one ("ok you can go now") still fires.
    # Closing it needs a leading-proper-noun heuristic on ASR-cased text, which
    # would also swallow the common real dismissal ("You can leave" — leading
    # capital, no name), so it's left as a documented trade-off, not a bug.
    leave_now = called and detect_leave_command(question)
    if settings.leave_on_command and not leave_now and not called:
        addressed = getattr(session, "last_addressed", None)
        if addressed is not None:
            a_speaker, a_ts, a_text = addressed
            if (
                speaker == a_speaker
                and time.time() - a_ts < 4.0
                and detect_leave_command(f"{a_text} {text}")
                and not _names_another_participant(
                    text, session.roster(avatar.name), avatar.wake_words
                )
            ):
                leave_now = True
    # Arm the split window ONLY for a bare address ("Cedric." / "hey Cedric");
    # a substantive addressed turn, a new speaker, or a stale (>4s) window clears
    # it, so the window can never linger into unrelated speech.
    if called:
        session.last_addressed = (
            (speaker, time.time(), text) if _address_is_bare(avatar, text) else None
        )
    elif getattr(session, "last_addressed", None) is not None and (
        speaker != session.last_addressed[0]
        or time.time() - session.last_addressed[1] >= 4.0
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
        and detect_closing(text)
        and len(session.transcript) >= 12
        and not session.in_cooldown(avatar.speak_cooldown_seconds)
    ):
        spoken_names = {u.speaker.strip().lower() for u in session.transcript}
        quiet = [
            n
            for n in session.roster(avatar.name)
            if n.strip().lower() not in spoken_names
            and not n.lower().startswith("guest")
        ]
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

    # ── deference window ──
    # Nobody addressed her by name, so this is at best a room-open question:
    # humans get first right of reply. Wait briefly; if anyone starts talking
    # (a partial lands or the transcript grows), yield silently. Deliberately
    # AFTER the cheap gates — a line that would be skipped anyway never waits.
    if not called and not followup and settings.deference_seconds > 0:
        _defer_mark = len(session.transcript)
        _defer_t0 = time.time()
        await asyncio.sleep(settings.deference_seconds)
        if (
            len(session.transcript) > _defer_mark
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
        item = tools.capture_action(session, question.strip())
        # ASR often splits one ask across finals ("Cedric, can you send" +
        # "the recap by Friday"). Remember this capture so a same-speaker
        # follow-up within a few seconds extends its text (see the
        # continuation check after wake detection).
        session.last_capture = (item, speaker, time.time())
        if session.speech_generation != turn_gen:  # barge-in since the final landed
            return JSONResponse({"ok": True, "spoke": False, "interrupted": True})
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
    _t_wake = time.perf_counter()
    spoke_any = False
    suppressed_any = False
    interrupted = False
    speak_tasks: list[asyncio.Task] = []
    prev_task: asyncio.Task | None = None
    async for sentence in iterate_in_threadpool(
        answer_question_stream(
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
        )
    ):
        # Interrupted (barge-in) or superseded by a newer turn while this
        # sentence was generating: abandon the rest of the answer. The page
        # already dropped the stale generation; don't keep paying for tokens.
        if session.speech_generation != turn_gen:
            interrupted = True
            break
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
