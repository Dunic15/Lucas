"""Northstar MVP demo HTTP surface — /org/demo/* + strict /dashboard/demo twin.

The smallest backend-owned entry points for the MVP journey: start a
meeting-bound demo, run the bounded coordinator, read safe status. org/principal
derive EXCLUSIVELY from the authenticated context (reusing the browser router's
machine gate + the dashboard cookie gate); a client-supplied org_id is a 403.
All routes 404 when the demo flag is off. No browser/approval/permission logic
lives here — it delegates to the canonical operator/coordinator.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import auth
from . import enabled, runtime

router = APIRouter(tags=["northstar-demo"])
_NO_STORE = {"Cache-Control": "no-store"}


def _disabled() -> JSONResponse:
    return JSONResponse({"error": "northstar_demo_disabled"},
                        status_code=404, headers=_NO_STORE)


async def _machine_gate(request: Request):
    from .. import org_api

    return await org_api._machine_gate(request)


def _org_mismatch(body: dict, org: str) -> bool:
    supplied = str((body or {}).get("org_id") or "").strip()
    return bool(supplied) and supplied != org


# ── ops ─────────────────────────────────────────────────────────────────────

def _op_start(org: str, principal: str, body: dict) -> tuple[int, dict]:
    result = runtime.start(
        org, principal=principal,
        meeting_ref=str((body or {}).get("meeting_ref") or ""),
        avatar_key=str((body or {}).get("avatar_key") or ""))
    if not result.get("ok"):
        return 404, result
    return 200, result


def _op_run(org: str, principal: str, session_id: str) -> tuple[int, dict]:
    result = runtime.run(org, session_id, principal=principal)
    if result.get("outcome") == "disabled":
        return 404, {"error": "northstar_demo_disabled"}
    return 200, result


def _op_status(org: str, principal: str, session_id: str) -> tuple[int, dict]:
    result = runtime.status(org, session_id, principal=principal)
    if result is None:
        return 404, {"error": "unknown session"}
    return 200, result


# ── machine surface: /org/demo/* ──────────────────────────────────────────────

async def _machine(request: Request, handler, *args,
                   needs_body: bool = False) -> JSONResponse:
    if not enabled():
        return _disabled()
    err, org = await _machine_gate(request)
    if err:
        return err
    if needs_body:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if _org_mismatch(body, org):
            return JSONResponse({"error": "org mismatch"}, status_code=403)
        args = (*args, body)
    code, payload = await run_in_threadpool(handler, org, "", *args)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.post("/org/demo/northstar/start")
async def org_demo_start(request: Request) -> JSONResponse:
    return await _machine(request, lambda org, pr, body: _op_start(org, pr, body),
                          needs_body=True)


@router.post("/org/demo/northstar/sessions/{sid}/run")
async def org_demo_run(sid: str, request: Request) -> JSONResponse:
    return await _machine(request, lambda org, pr: _op_run(org, pr, sid))


@router.get("/org/demo/northstar/sessions/{sid}")
async def org_demo_status(sid: str, request: Request) -> JSONResponse:
    return await _machine(request, lambda org, pr: _op_status(org, pr, sid))


# ── dashboard twin (cookie + same-origin) ─────────────────────────────────────

@router.api_route("/dashboard/demo/northstar/{tail:path}",
                  methods=["GET", "POST"])
async def dashboard_demo(tail: str, request: Request) -> JSONResponse:
    if not enabled():
        return _disabled()
    user = auth.current_user(request)
    if user is None:
        if err := auth.gate(request):
            return err
        return JSONResponse({"error": "login required"}, status_code=401)
    org = str(user.get("org_id") or "")
    principal = str(user.get("user_id") or "")
    if request.method != "GET" and not auth._same_origin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body: dict = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if _org_mismatch(body, org):
            return JSONResponse({"error": "org mismatch"}, status_code=403)
    parts = [p for p in (tail or "").split("/") if p]
    method = request.method

    def run() -> tuple[int, dict]:
        if parts == ["start"] and method == "POST":
            return _op_start(org, principal, body)
        if (len(parts) == 3 and parts[0] == "sessions"
                and parts[2] == "run" and method == "POST"):
            return _op_run(org, principal, parts[1])
        if len(parts) == 2 and parts[0] == "sessions" and method == "GET":
            return _op_status(org, principal, parts[1])
        return 404, {"error": "unknown demo route"}

    code, payload = await run_in_threadpool(run)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)
