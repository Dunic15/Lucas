from __future__ import annotations
"""Demo + live console REST — /, /demo/*, /live/*. Non-meeting endpoints that
exercise the brain/RAG directly (key-free demo + live-page assist). Extracted
from main.py; the live-meeting contract (ws/webhooks) stays in main.py."""
import asyncio, json

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from starlette.concurrency import iterate_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import avatars, anam_client, auth
from app.brain.engine import (
    answer_question, answer_question_stream, answer_with_tools, wants_web_search, post_meeting,
)
from app.core.config import REPO_ROOT
from app.api.deps import _line_for

router = APIRouter()
FRONTEND_DIR = REPO_ROOT / "frontend"


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


_ACK_FILLER_AFTER_S = 1.2


class AskRequest(BaseModel):
    question: str
    avatar_id: str = "laura"


class PostMeetingRequest(BaseModel):
    transcript: str
    avatar_id: str = "laura"


class LiveTokenRequest(BaseModel):
    avatar_id: str = "laura"


class LiveError(BaseModel):
    where: str = ""
    message: str = ""
    stack: str = ""


@router.get("/")
def demo_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "demo.html")


@router.post("/demo/ask")
async def demo_ask(req: AskRequest) -> JSONResponse:
    """Ask an avatar a question → grounded, cited answer (no meeting needed)."""
    avatar = avatars.load(req.avatar_id)  # raises if unknown
    result = await run_in_threadpool(answer_question, avatar, req.question)
    return JSONResponse(result)


@router.post("/live/ask")
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
            answer_question_stream(avatar, req.question, mission=avatar.mission)
        ):
            yield f"data: {json.dumps({'content': sentence})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/live/act")
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


@router.post("/demo/post_meeting")
async def demo_post_meeting(req: PostMeetingRequest) -> JSONResponse:
    """Turn a meeting transcript into the full post-meeting artifact."""
    try:
        avatar = avatars.load(req.avatar_id)
    except FileNotFoundError:
        # A bogus avatar_id would otherwise raise an unhandled 500 and the demo
        # page shows a bare "HTTP 500" — answer a clear 404 with the choices.
        return JSONResponse(
            {"error": "unknown avatar_id", "available": avatars.list_ids()},
            status_code=404,
        )
    artifact = await run_in_threadpool(post_meeting, avatar, req.transcript)
    # Echo the transcript so the demo artifact matches the live one
    # (_finalize_session does the same); the page shows it in a transcript tab.
    artifact["transcript"] = req.transcript
    return JSONResponse(artifact)


@router.get("/demo/sample")
def demo_sample(avatar_id: str = "laura") -> JSONResponse:
    """A sample transcript to load into the post-meeting demo, if the avatar has one."""
    try:
        avatar = avatars.load(avatar_id)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "unknown avatar_id", "available": avatars.list_ids()},
            status_code=404,
        )
    sample = avatar.dir / "sample_meeting.txt"
    text = sample.read_text() if sample.exists() else ""
    return JSONResponse({"avatar_id": avatar_id, "transcript": text})


@router.post("/live/error")
async def live_error(e: LiveError) -> JSONResponse:
    """The avatar page reports client-side Anam errors here so we can see them."""
    print(f"\n[LIVE-ERROR] {e.where}: {e.message}\n{(e.stack or '')[:1500]}\n", flush=True)
    return JSONResponse({"ok": True})


@router.post("/live/token")
async def live_token(req: LiveTokenRequest, request: Request) -> JSONResponse:
    """Mint a fresh Anam session token for the browser to stream the avatar.
    Gated: minting an Anam conversation bills per-minute, so an anonymous caller
    can't rack up charges — a logged-in owner (or the machine bearer) only. Open
    in the key-free demo (auth disabled). The current /talk face uses TalkingHead
    + /tts (not Anam), so this only affects the legacy /live + /avatar pages."""
    if auth.current_user(request) is None:
        if err := auth.gate(request):
            return err
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
