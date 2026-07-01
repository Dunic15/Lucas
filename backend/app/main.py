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

from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import avatars, store, recall_client, anam_client, granola_client
from .brain import answer_question, post_meeting, effective_provider
from .config import settings
from .decision import detect_wake, passes_confidence
from .rag import ensure_index

app = FastAPI(title="Callable AI Process Avatar")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@app.on_event("startup")
def _prebuild_indexes() -> None:
    """Build each avatar's RAG index on boot so the demo works with no setup.

    Free & instant with the default hash embedder; skipped if already current.
    """
    for aid in avatars.list_ids():
        try:
            ensure_index(avatars.load(aid))
        except Exception as e:  # a bad avatar shouldn't stop the server
            print(f"[startup] could not index avatar '{aid}': {e}")


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
    avatar_id: str = "lucas"


@app.post("/demo/ask")
async def demo_ask(req: AskRequest) -> JSONResponse:
    """Ask an avatar a question → grounded, cited answer (no meeting needed)."""
    avatar = avatars.load(req.avatar_id)  # raises if unknown
    result = await run_in_threadpool(answer_question, avatar, req.question)
    return JSONResponse(result)


class PostMeetingRequest(BaseModel):
    transcript: str
    avatar_id: str = "lucas"


@app.post("/demo/post_meeting")
async def demo_post_meeting(req: PostMeetingRequest) -> JSONResponse:
    """Turn a meeting transcript into summary + gap checklist + follow-up email."""
    avatar = avatars.load(req.avatar_id)
    artifact = await run_in_threadpool(post_meeting, avatar, req.transcript)
    return JSONResponse(artifact)


@app.get("/demo/sample")
def demo_sample(avatar_id: str = "lucas") -> JSONResponse:
    """A sample transcript to load into the post-meeting demo, if the avatar has one."""
    avatar = avatars.load(avatar_id)
    sample = avatar.dir / "sample_meeting.txt"
    text = sample.read_text() if sample.exists() else ""
    return JSONResponse({"avatar_id": avatar_id, "transcript": text})


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
    avatar_id: str = "lucas"


@app.post("/sessions/start")
async def start_session(req: StartRequest) -> JSONResponse:
    avatar = avatars.load(req.avatar_id)  # raises if unknown

    # 1. Avatar: persona (ElevenLabs voice) + live session (Anam session token).
    persona_id = await run_in_threadpool(anam_client.create_persona, avatar)
    convo = await run_in_threadpool(
        anam_client.create_conversation, avatar, persona_id
    )

    # 2. Avatar page URL the Recall bot will render as its camera.
    #    The page keys its websocket on conversation_id (the only id it knows
    #    at render time — the bot id doesn't exist yet here).
    from urllib.parse import quote

    avatar_url = (
        f"{settings.public_base_url.rstrip('/')}/avatar"
        f"?conversation_url={quote(convo['conversation_url'], safe='')}"
        f"&conversation_id={convo['conversation_id']}"
    )

    # 3. Send the bot in.
    bot = await run_in_threadpool(
        recall_client.create_bot, req.meeting_url, avatar_url
    )

    session = store.create(
        bot_id=bot["id"], meeting_url=req.meeting_url, avatar_id=avatar.id
    )
    session.anam_conversation_id = convo["conversation_id"]
    session.anam_conversation_url = convo["conversation_url"]
    store.register_conversation(convo["conversation_id"], bot["id"])

    return JSONResponse(
        {
            "bot_id": bot["id"],
            "anam_conversation_id": convo["conversation_id"],
            "avatar_page_url": avatar_url,
        }
    )


@app.post("/sessions/{bot_id}/end")
async def end_session(bot_id: str) -> JSONResponse:
    session = store.get(bot_id)
    if session is None:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)

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

    store.remove(bot_id)
    return JSONResponse(artifact)


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


async def _make_avatar_speak(session: store.Session, text: str) -> None:
    if session.ws is not None:
        await session.ws.send_json({"type": "speak", "text": text})
        session.mark_spoke()


# ───────────────────────── recall webhook ──────────────────────────
@app.post("/webhooks/recall")
async def recall_webhook(request: Request) -> JSONResponse:
    payload = await request.json()
    event = payload.get("event", "")

    if event != "transcript.data":
        # Other events (bot status, participant join/leave) ignored for MVP.
        return JSONResponse({"ok": True, "ignored": event})

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

    # ── when-to-speak gate ──
    called, question = detect_wake(avatar, text)
    if not called:
        return JSONResponse({"ok": True, "spoke": False, "reason": "not called"})
    if session.in_cooldown(avatar.speak_cooldown_seconds):
        return JSONResponse({"ok": True, "spoke": False, "reason": "cooldown"})

    result = await run_in_threadpool(answer_question, avatar, question or text)
    if not passes_confidence(avatar, result):
        return JSONResponse(
            {"ok": True, "spoke": False, "reason": "low confidence", "result": result}
        )

    await _make_avatar_speak(session, result["answer"])
    return JSONResponse(
        {"ok": True, "spoke": True, "answer": result["answer"],
         "citations": result.get("citations", [])}
    )
