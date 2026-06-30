"""Callable AI Process Avatar — backend.

Flow:
  POST /sessions/start  -> create Tavus avatar (ElevenLabs voice) + send Recall
                           bot into the meeting rendering our avatar page.
  Recall  --transcript.data-->  POST /webhooks/recall
                           -> store utterance, run the when-to-speak gate.
                           -> if the avatar is called & confident, push the
                              answer over the websocket to the avatar page,
                              which makes Tavus speak it (echo interaction).
  POST /sessions/{id}/end -> remove bot, end avatar, return post-meeting
                             summary + gap checklist + draft follow-up email.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import store, recall_client, tavus_client
from .brain import answer_question, post_meeting
from .config import settings
from .decision import detect_wake, passes_confidence

app = FastAPI(title="Callable AI Process Avatar")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


# ───────────────────────────── health ──────────────────────────────
@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "active_sessions": len(store.all_sessions()),
        "brain_model": settings.brain_model,
        "wake_words": settings.wake_word_list,
    }


# ──────────────────────── session lifecycle ────────────────────────
class StartRequest(BaseModel):
    meeting_url: str


@app.post("/sessions/start")
async def start_session(req: StartRequest) -> JSONResponse:
    # 1. Avatar: persona (ElevenLabs voice) + live conversation (Daily room).
    persona_id = await run_in_threadpool(tavus_client.ensure_persona)
    convo = await run_in_threadpool(tavus_client.create_conversation, persona_id)

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

    session = store.create(bot_id=bot["id"], meeting_url=req.meeting_url)
    session.tavus_conversation_id = convo["conversation_id"]
    session.tavus_conversation_url = convo["conversation_url"]
    store.register_conversation(convo["conversation_id"], bot["id"])

    return JSONResponse(
        {
            "bot_id": bot["id"],
            "tavus_conversation_id": convo["conversation_id"],
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
    if session.tavus_conversation_id:
        await run_in_threadpool(
            tavus_client.end_conversation, session.tavus_conversation_id
        )

    artifact: dict = {"summary": "", "checklist": [], "follow_up_email": {}}
    if transcript_text.strip():
        artifact = await run_in_threadpool(post_meeting, transcript_text)

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

    # ── when-to-speak gate ──
    called, question = detect_wake(text)
    if not called:
        return JSONResponse({"ok": True, "spoke": False, "reason": "not called"})
    if session.in_cooldown(settings.speak_cooldown_seconds):
        return JSONResponse({"ok": True, "spoke": False, "reason": "cooldown"})

    result = await run_in_threadpool(answer_question, question or text)
    if not passes_confidence(result):
        return JSONResponse(
            {"ok": True, "spoke": False, "reason": "low confidence", "result": result}
        )

    await _make_avatar_speak(session, result["answer"])
    return JSONResponse(
        {"ok": True, "spoke": True, "answer": result["answer"],
         "citations": result.get("citations", [])}
    )
