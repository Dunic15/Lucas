"""Dashboard and tool-bridge endpoints for the OpenClaw experiment."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import auth, cedric
from ..config import settings
from . import gates, runtime

router = APIRouter(tags=["openclaw"])
_NO_STORE = {"Cache-Control": "no-store"}


async def _dashboard_org(request: Request) -> tuple[JSONResponse | None, str]:
    user = auth.current_user(request)
    if user is not None:
        return None, str(user.get("org_id") or "")
    org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if org is None:
        if err := auth.gate(request):
            return err, ""
        org = settings.demo_org_id
    return None, org or ""


def _auth_header(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if value:
        return value
    value = request.headers.get("x-openclaw-capability", "")
    return f"Bearer {value}" if value else ""


@router.get("/dashboard/openclaw/runs")
async def dashboard_openclaw_runs(request: Request, limit: int = 20) -> JSONResponse:
    err, org = await _dashboard_org(request)
    if err:
        return err
    status = gates.snapshot(org)
    runs = await run_in_threadpool(runtime.list_runs, org, limit)
    return JSONResponse(
        {"ok": True, "org_id": org, **status, "runs": runs},
        headers=_NO_STORE,
    )


@router.get("/dashboard/openclaw/runs/{run_id}")
async def dashboard_openclaw_run(run_id: str, request: Request) -> JSONResponse:
    err, org = await _dashboard_org(request)
    if err:
        return err
    detail = await run_in_threadpool(runtime.run_detail, org, run_id)
    if detail is None:
        return JSONResponse({"error": "unknown OpenClaw run"}, status_code=404)
    return JSONResponse({"ok": True, "run": detail}, headers=_NO_STORE)


@router.post("/dashboard/openclaw/runs/{run_id}/cancel")
async def dashboard_openclaw_cancel(run_id: str, request: Request) -> JSONResponse:
    err, org = await _dashboard_org(request)
    if err:
        return err
    if not auth._same_origin(request):
        return JSONResponse({"error": "same-origin required"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    reason = str((body if isinstance(body, dict) else {}).get("reason") or "cancelled")
    ok = await run_in_threadpool(runtime.cancel_run, org, run_id, reason=reason)
    if not ok:
        return JSONResponse({"error": "unknown OpenClaw run"}, status_code=404)
    return JSONResponse({"ok": True}, headers=_NO_STORE)


@router.post("/dashboard/openclaw/replay")
async def dashboard_openclaw_replay(request: Request) -> JSONResponse:
    err, org = await _dashboard_org(request)
    if err:
        return err
    if not auth._same_origin(request):
        return JSONResponse({"error": "same-origin required"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    body = body if isinstance(body, dict) else {}
    meeting_id = str(body.get("meeting_id") or body.get("bot_id") or "").strip()
    if not meeting_id:
        return JSONResponse({"error": "missing meeting_id"}, status_code=400)
    result = await run_in_threadpool(runtime.replay_meeting, org, meeting_id)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status_code, headers=_NO_STORE)


@router.get("/dashboard/openclaw/chats")
async def dashboard_openclaw_chats(request: Request, limit: int = 50) -> JSONResponse:
    err, org = await _dashboard_org(request)
    if err:
        return err
    threads = await run_in_threadpool(runtime.list_chat_threads, org, limit)
    return JSONResponse({"ok": True, "threads": threads}, headers=_NO_STORE)


@router.post("/dashboard/openclaw/chats")
async def dashboard_openclaw_chat_create(request: Request) -> JSONResponse:
    err, org = await _dashboard_org(request)
    if err:
        return err
    if not auth._same_origin(request):
        return JSONResponse({"error": "same-origin required"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    body = body if isinstance(body, dict) else {}
    workflow_id = str(body.get("workflow_id") or "").strip()
    seed = None
    meeting_id = str(body.get("meeting_id") or "")
    title = str(body.get("title") or "")
    if workflow_id:
        context = await run_in_threadpool(
            runtime.saved_chat_workflow_context, org, workflow_id
        )
        if context is None:
            return JSONResponse(
                {"error": "unknown workflow for this workspace"},
                status_code=404,
            )
        seed = context["workflow"]
        meeting_id = str(context.get("meeting_id") or meeting_id)
    thread = await run_in_threadpool(
        runtime.create_chat_thread,
        org,
        meeting_id=meeting_id,
        title=title,
        seed_workflow=seed,
    )
    if thread is None:
        return JSONResponse(
            {"error": "OpenClaw chat is unavailable"}, status_code=400
        )
    return JSONResponse({"ok": True, "thread": thread}, headers=_NO_STORE)


@router.get("/dashboard/openclaw/chats/{thread_id}")
async def dashboard_openclaw_chat_detail(
    thread_id: str, request: Request
) -> JSONResponse:
    err, org = await _dashboard_org(request)
    if err:
        return err
    thread = await run_in_threadpool(
        runtime.chat_thread_detail, org, thread_id
    )
    if thread is None:
        return JSONResponse(
            {"error": "unknown chat for this workspace"}, status_code=404
        )
    return JSONResponse({"ok": True, "thread": thread}, headers=_NO_STORE)


@router.post("/dashboard/openclaw/chat")
async def dashboard_openclaw_chat(request: Request) -> JSONResponse:
    err, org = await _dashboard_org(request)
    if err:
        return err
    if not auth._same_origin(request):
        return JSONResponse({"error": "same-origin required"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    body = body if isinstance(body, dict) else {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    thread_id = str(body.get("thread_id") or "").strip()
    thread = None
    history = body.get("history") if isinstance(body.get("history"), list) else []
    meeting_id = str(body.get("meeting_id") or "")
    if thread_id:
        thread = await run_in_threadpool(
            runtime.get_chat_thread, org, thread_id
        )
        if thread is None:
            return JSONResponse(
                {"error": "unknown chat for this workspace"}, status_code=404
            )
        history = await run_in_threadpool(
            runtime.chat_thread_history, org, thread_id, 8
        )
        if "meeting_id" not in body:
            meeting_id = str(thread.get("meeting_id") or "")
        stored = await run_in_threadpool(
            runtime.add_chat_thread_message,
            org,
            thread_id,
            "user",
            message,
            meeting_id=meeting_id,
        )
        if stored is None:
            return JSONResponse(
                {"error": "unknown chat for this workspace"}, status_code=404
            )
    result = await run_in_threadpool(
        runtime.chat,
        org,
        message,
        meeting_id=meeting_id,
        history=history,
        thread_id=thread_id,
    )
    if thread_id and result.get("ok"):
        await run_in_threadpool(
            runtime.add_chat_thread_message,
            org,
            thread_id,
            "assistant",
            str(result.get("reply") or "I prepared the result."),
            workflow=result.get("workflow"),
        )
        result["thread"] = await run_in_threadpool(
            runtime.get_chat_thread, org, thread_id
        )
    return JSONResponse(
        result,
        status_code=200 if result.get("ok") else 400,
        headers=_NO_STORE,
    )


@router.post("/dashboard/openclaw/workflows/start")
async def dashboard_openclaw_workflow_start(request: Request) -> JSONResponse:
    err, org = await _dashboard_org(request)
    if err:
        return err
    if not auth._same_origin(request):
        return JSONResponse({"error": "same-origin required"}, status_code=403)
    user = auth.current_user(request)
    if user is None:
        return JSONResponse({"error": "login required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    body = body if isinstance(body, dict) else {}
    workflow = body.get("workflow") if isinstance(body.get("workflow"), dict) else {}
    source_thread_id = str(body.get("thread_id") or "").strip()
    source_meeting_id = ""
    if source_thread_id:
        thread = await run_in_threadpool(
            runtime.get_chat_thread, org, source_thread_id
        )
        if thread is None:
            return JSONResponse(
                {"error": "unknown chat for this workspace"}, status_code=404
            )
        source_meeting_id = str(thread.get("meeting_id") or "")
    result = await run_in_threadpool(
        runtime.start_chat_workflow,
        org,
        workflow,
        laura_user_id=str(user.get("user_id") or ""),
        source_thread_id=source_thread_id,
        source_meeting_id=source_meeting_id,
    )
    return JSONResponse(
        result,
        status_code=200 if result.get("ok") else 400,
        headers=_NO_STORE,
    )


def _http_status(result: dict) -> int:
    """HTTP code for a runtime result — only an INTEGER ``status`` is one.

    ``run_tool`` overloads ``status``: on a refusal it is an HTTP code (401,
    403, 409), on a completed call it is the ACTION status ("done",
    "needs_attention", "running"...). Popping and int()-ing it blindly turned
    every successful tool call into a 500 (``int("done")`` raises). An action
    status stays in the body, where the gateway reads it.
    """
    raw = result.get("status", 200)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 200
    result.pop("status", None)
    return raw or 200


@router.post("/openclaw/tools/{tool_name}")
async def openclaw_tool(tool_name: str, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    result = await run_in_threadpool(
        runtime.run_tool, _auth_header(request), tool_name, body if isinstance(body, dict) else {}
    )
    return JSONResponse(
        result, status_code=_http_status(result), headers=_NO_STORE
    )


@router.post("/openclaw/runs/{run_id}/events")
async def openclaw_event(run_id: str, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = body if isinstance(body, dict) else {}
    result = await run_in_threadpool(
        runtime.record_gateway_event, _auth_header(request), {**body, "run_id": run_id}
    )
    return JSONResponse(
        result, status_code=_http_status(result), headers=_NO_STORE
    )
