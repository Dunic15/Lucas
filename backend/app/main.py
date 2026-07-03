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
import re
import time
import uuid
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
    StreamingResponse,
)
from pydantic import BaseModel

from . import (
    avatars,
    store,
    recall_client,
    anam_client,
    granola_client,
    actions,
    gmail_watcher,
)
from .brain import (
    answer_question,
    answer_question_stream,
    post_meeting,
    proactive_flag,
    effective_provider,
)
from .config import settings
from .decision import detect_wake, detect_closing
from .rag import ensure_index, warm as warm_index

app = FastAPI(title="Callable AI Process Avatar")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
GOOGLE_CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    # Read Laura's inbox so "Add people" invites (which email her a Meet link,
    # with no calendar event) can auto-join the meeting. See gmail_watcher.py.
    "https://www.googleapis.com/auth/gmail.readonly",
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


@app.on_event("startup")
def _prebuild_indexes() -> None:
    """Build each avatar's RAG index on boot so the demo works with no setup.

    Free & instant with the default hash embedder; skipped if already current.
    """
    for aid in avatars.list_ids():
        try:
            avatar = avatars.load(aid)
            ensure_index(avatar)
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

_MEET_CODE_RE = re.compile(r"meet\.google\.com/([a-z-]+)")
_BOT_TERMINAL = {"call_ended", "done", "fatal"}
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
    topics = "onboarding, access/security, or the AI Buffer thesis"
    return (
        f"I can hear you, but I need a specific question about {topics}. "
        f"Try: {avatar.name}, what approval step is required?"
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
            for _mid, url in new:
                if _shutting_down:
                    break  # draining — don't start new bots
                if store.is_scheduled(url):
                    continue
                # Durable cross-instance guard against duplicate bots.
                if await run_in_threadpool(_meeting_has_active_bot, url):
                    store.mark_scheduled(url)
                    continue
                try:
                    res = await _start_avatar_session(url, "laura")
                    store.mark_scheduled(url)
                    _gmail_state["joined"].append(
                        {"meeting_url": url, "bot_id": res["bot_id"], "at": time.time()}
                    )
                    print(f"[gmail-watch] joined {url} via bot {res['bot_id']}", flush=True)
                    # Resolve any deploy-overlap duplicate: let a racing bot register,
                    # then keep the best Recall variant and drop the rest.
                    await asyncio.sleep(4)
                    await run_in_threadpool(_reconcile_duplicate_bots, url, res["bot_id"])
                except Exception as e:
                    print(f"[gmail-watch] failed to join {url}: {e}", flush=True)
        except Exception as e:
            _gmail_state["last_error"] = str(e)


@app.on_event("startup")
async def _launch_gmail_watch() -> None:
    if settings.gmail_watch_enabled:
        asyncio.create_task(_gmail_watch_loop())


@app.on_event("shutdown")
async def _drain_gmail_watch() -> None:
    """On a deploy/rollout App Runner sends SIGTERM to the old instance; flip the
    drain flag so its Gmail watcher stops dispatching at once. Combined with the
    per-dispatch Recall pre-check and the variant-aware reconcile, this stops the
    old + new instances from both putting a bot in the same meeting during overlap.
    """
    global _shutting_down
    _shutting_down = True
    print("[gmail-watch] shutdown signal — watcher draining", flush=True)


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


class PostMeetingRequest(BaseModel):
    transcript: str
    avatar_id: str = "laura"


@app.post("/demo/post_meeting")
async def demo_post_meeting(req: PostMeetingRequest) -> JSONResponse:
    """Turn a meeting transcript into summary + gap checklist + follow-up email."""
    avatar = avatars.load(req.avatar_id)
    artifact = await run_in_threadpool(post_meeting, avatar, req.transcript)
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
    avatar_id: str = "laura"
    join_at: Optional[str] = None  # ISO 8601; set (>=10 min out) to schedule the bot


async def _start_avatar_session(
    meeting_url: str, avatar_id: str = "laura", join_at: Optional[str] = None
) -> dict:
    """Send a Recall bot (rendering the avatar page as its camera) into a meeting.

    Shared by the manual /sessions/start endpoint, the calendar auto-join webhook,
    and the Gmail watcher. Raises on failure. The avatar page mints its own fresh
    Anam token at render time and keys its websocket on the conversation_id.
    """
    avatar = avatars.load(avatar_id)  # raises if unknown
    conversation_id = uuid.uuid4().hex
    avatar_url = (
        f"{settings.public_base_url.rstrip('/')}/avatar"
        f"?avatar_id={avatar.id}&conversation_id={conversation_id}"
    )
    bot = await run_in_threadpool(
        recall_client.create_bot, meeting_url, avatar_url, join_at
    )
    session = store.create(
        bot_id=bot["id"], meeting_url=meeting_url, avatar_id=avatar.id
    )
    session.anam_conversation_id = conversation_id
    store.register_conversation(conversation_id, bot["id"])
    return {
        "bot_id": bot["id"],
        "conversation_id": conversation_id,
        "avatar_page_url": avatar_url,
        "scheduled_for": join_at,
    }


@app.post("/sessions/start")
async def start_session(req: StartRequest) -> JSONResponse:
    try:
        recall_client.assert_ready()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        result = await _start_avatar_session(req.meeting_url, req.avatar_id, req.join_at)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(result)


async def _finalize_session(bot_id: str) -> dict | None:
    """End a session once: stop both vendors, build + store the artifact.

    Idempotent — if the session is already gone, returns the stored artifact (or
    None). Safe to call from the manual endpoint AND the auto end-of-meeting hook.
    """
    session = store.get(bot_id)
    if session is None:
        return store.get_artifact(bot_id)

    transcript_text = session.transcript_text()

    # Stop billing on both vendors.
    await run_in_threadpool(recall_client.leave_call, bot_id)
    if session.anam_conversation_id:
        await run_in_threadpool(
            anam_client.end_conversation, session.anam_conversation_id
        )

    artifact: dict = {"summary": "", "checklist": [], "follow_up_email": {}}
    if transcript_text.strip():
        avatar = avatars.load(session.avatar_id)
        artifact = await run_in_threadpool(post_meeting, avatar, transcript_text)

    store.save_artifact(bot_id, artifact)
    store.remove(bot_id)
    return artifact


@app.post("/sessions/{bot_id}/end")
async def end_session(bot_id: str) -> JSONResponse:
    artifact = await _finalize_session(bot_id)
    if artifact is None:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    return JSONResponse(artifact)


class DeliverRequest(BaseModel):
    to: list[str] = []
    slack: bool = True


@app.post("/sessions/{bot_id}/deliver")
async def deliver_artifact(bot_id: str, req: DeliverRequest) -> JSONResponse:
    """Actually send the finished meeting's follow-up email + post it to Slack."""
    artifact = store.get_artifact(bot_id)
    if artifact is None:
        return JSONResponse({"error": "no artifact for this bot_id"}, status_code=404)

    avatar = avatars.load(store.get(bot_id).avatar_id) if store.get(bot_id) else None
    name = avatar.name if avatar else "Laura"
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


@app.get("/sessions/{bot_id}/artifact")
def session_artifact(bot_id: str) -> JSONResponse:
    """Retrieve a finished session's artifact (summary + checklist + email)."""
    live = store.get(bot_id)
    if live is not None:
        return JSONResponse({"status": "in_progress", "bot_id": bot_id})
    artifact = store.get_artifact(bot_id)
    if artifact is None:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)
    return JSONResponse({"status": "done", **artifact})


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


async def _make_avatar_speak(
    session: store.Session, text: str, citations: list | None = None
) -> None:
    """Backend-as-brain: send the exact words for the avatar to speak (Anam talk)."""
    if session.ws is not None:
        await session.ws.send_json(
            {"type": "speak", "text": text, "citations": citations or []}
        )
        session.mark_spoke()


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
    return bool(_extract_invite_emails(event) & target_emails)


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
        avatar_id = ev.get("avatar_id") or "laura"
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
                f"{settings.public_base_url.rstrip('/')}/avatar"
                f"?avatar_id={avatar.id}&conversation_id={conversation_id}"
            )
            bot = await run_in_threadpool(
                recall_client.create_bot, url, avatar_url, start
            )
            s = store.create(bot_id=bot["id"], meeting_url=url, avatar_id=avatar.id)
            s.anam_conversation_id = conversation_id
            store.register_conversation(conversation_id, bot["id"])
            if eid:
                store.mark_scheduled(eid)
            scheduled.append({"event": eid, "bot_id": bot["id"], "join_at": start})
        except Exception as e:  # one bad event shouldn't drop the webhook
            scheduled.append({"event": eid, "error": str(e)})
    return JSONResponse({"ok": True, "scheduled": scheduled})


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

    if event != "transcript.data":
        # Auto end-of-meeting: when Recall reports the call is over / bot done,
        # finalize the session (stop billing on both vendors + build the artifact).
        # NOTE: bot status-change events are delivered to the webhook configured
        # in the Recall dashboard — point it at PUBLIC_BASE_URL/webhooks/recall.
        TERMINAL = {"done", "call_ended", "fatal", "bot.call_ended", "bot.done"}
        status_code = (
            payload.get("data", {}).get("status", {}).get("code")
            or payload.get("data", {}).get("code")
            or ""
        )
        term = event in TERMINAL or status_code in TERMINAL
        bid = payload.get("data", {}).get("bot", {}).get("id", "") or payload.get(
            "data", {}
        ).get("bot_id", "")
        if term and bid and store.get(bid) is not None:
            await _finalize_session(bid)
            return JSONResponse({"ok": True, "finalized": bid})
        return JSONResponse({"ok": True, "ignored": event or status_code})

    data = payload.get("data", {}).get("data", {})
    bot_id = payload.get("data", {}).get("bot", {}).get("id", "")
    session = store.get(bot_id)
    if session is None:
        return JSONResponse({"ok": True, "note": "no session"})

    words = data.get("words", [])
    text = " ".join(w.get("text", "") for w in words).strip()
    speaker = (data.get("participant") or {}).get("name") or "Unknown"
    if not text:
        return JSONResponse({"ok": True})

    session.add_utterance(speaker, text)
    avatar = avatars.load(session.avatar_id)

    # ── proactive intervention (fires once, as the meeting wraps up) ──
    if (
        settings.proactive_enabled
        and not session.proactive_done
        and detect_closing(text)
        and not session.in_cooldown(avatar.speak_cooldown_seconds)
    ):
        flag = await run_in_threadpool(
            proactive_flag, avatar, session.transcript_text()
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
    called, question = detect_wake(avatar, text)
    if settings.require_wake_word and not called:
        return JSONResponse({"ok": True, "spoke": False, "reason": "not called"})
    question = question or text  # no wake word → treat the whole utterance as the ask
    if session.in_cooldown(avatar.speak_cooldown_seconds):
        return JSONResponse({"ok": True, "spoke": False, "reason": "cooldown"})

    # Backend is the brain: answer from OUR knowledge (RAG) with the recent
    # meeting conversation as context. Streamed sentence-by-sentence so the avatar
    # starts speaking on the first sentence instead of waiting for the whole
    # answer. Grounding is enforced by the SKIP sentinel inside the stream: if the
    # context is insufficient the generator yields nothing and the avatar stays
    # silent (the streaming equivalent of the old confidence gate).
    history = session.recent_transcript(n=8)
    _t_wake = time.perf_counter()
    spoke_any = False
    async for sentence in iterate_in_threadpool(
        answer_question_stream(avatar, question or text, history=history)
    ):
        if not spoke_any:
            print(
                f"[latency] wake->first_speak="
                f"{(time.perf_counter() - _t_wake) * 1000:.0f}ms",
                flush=True,
            )
        await _make_avatar_speak(session, sentence)
        spoke_any = True

    if not spoke_any:
        if _should_repair_silent_answer(called, text):
            line = _silent_answer_repair_line(avatar)
            await _make_avatar_speak(session, line)
            return JSONResponse(
                {
                    "ok": True,
                    "spoke": True,
                    "reason": "repair_after_skip",
                    "line": line,
                }
            )
        return JSONResponse(
            {"ok": True, "spoke": False, "reason": "insufficient context (SKIP)"}
        )
    return JSONResponse({"ok": True, "spoke": True, "streamed": True})
