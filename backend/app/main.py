"""Callable AI Process Avatar — backend.

Flow:
  POST /sessions/start  -> send Recall bot into the meeting rendering our
                           Anam avatar page.
  Recall  --transcript.data-->  POST /webhooks/recall
                           -> store utterance, run the when-to-speak gate.
                           -> if the avatar is called & confident, push the
                              answer over the websocket to the avatar page,
                              which makes Anam speak it (`talk` command).
  POST /sessions/{id}/end -> remove bot and return post-meeting summary,
                             gap checklist, and draft follow-up email.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import avatars, store, recall_client, anam_client
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


# ──────────────────────── session lifecycle ────────────────────────
class StartRequest(BaseModel):
    meeting_url: str
    avatar_id: str = "sofia"


@app.post("/sessions/start")
async def start_session(req: StartRequest) -> JSONResponse:
    avatar = avatars.load(req.avatar_id)  # raises if unknown

    # 1. Avatar page URL the Recall bot will render as its camera. The page
    #    uses this local id to request an Anam session token and open a websocket.
    anam_session_id = uuid4().hex
    avatar_url = (
        f"{settings.public_base_url.rstrip('/')}/avatar"
        f"?anam_session_id={anam_session_id}"
    )

    # 2. Send the bot in.
    bot = await run_in_threadpool(
        recall_client.create_bot, req.meeting_url, avatar_url
    )

    session = store.create(
        bot_id=bot["id"], meeting_url=req.meeting_url, avatar_id=avatar.id
    )
    session.anam_session_id = anam_session_id
    store.register_anam_session(anam_session_id, bot["id"])

    return JSONResponse(
        {
            "bot_id": bot["id"],
            "anam_session_id": anam_session_id,
            "avatar_page_url": avatar_url,
        }
    )


@app.post("/sessions/{bot_id}/end")
async def end_session(bot_id: str) -> JSONResponse:
    session = store.get(bot_id)
    if session is None:
        return JSONResponse({"error": "unknown bot_id"}, status_code=404)

    transcript_text = session.transcript_text()

    # Stop Recall. The Anam browser stream closes when the rendered page unloads.
    await run_in_threadpool(recall_client.leave_call, bot_id)

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


@app.post("/anam/session-token/{anam_session_id}")
async def anam_session_token(anam_session_id: str) -> JSONResponse:
    session = store.get_by_anam_session(anam_session_id)
    if session is None:
        return JSONResponse({"error": "unknown anam_session_id"}, status_code=404)

    avatar = avatars.load(session.avatar_id)
    token = await run_in_threadpool(anam_client.create_session_token, avatar)
    return JSONResponse({"sessionToken": token})


@app.websocket("/ws/{anam_session_id}")
async def avatar_ws(websocket: WebSocket, anam_session_id: str) -> None:
    await websocket.accept()
    session = store.get_by_anam_session(anam_session_id)
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
