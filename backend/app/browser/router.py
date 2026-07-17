"""Browser operator HTTP surface (B0): /org/browser/* + strict dashboard twin.

org_id and principal derive EXCLUSIVELY from the authenticated context; a
client-supplied org_id in any body is a 403 on mismatch. Writes on the
dashboard twin require cookie + same-origin + login; every branch checks its
METHOD. Provider ids and presentation tokens never appear in responses beyond
the single mint (token) / exchange (viewer) — and are never logged. All
routes 404 when the feature is off, preserving the key-free demo exactly.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import auth
from . import dal, enabled, operator
from .provider import ProviderUnconfigured

router = APIRouter(tags=["browser-operator"])
_NO_STORE = {"Cache-Control": "no-store"}


def _disabled() -> JSONResponse:
    return JSONResponse({"error": "browser_operator_disabled"},
                        status_code=404, headers=_NO_STORE)


async def _machine_gate(request: Request):
    from .. import org_api

    return await org_api._machine_gate(request)


def _org_mismatch(body: dict, org: str) -> bool:
    supplied = str((body or {}).get("org_id") or "").strip()
    return bool(supplied) and supplied != org


# ── shared sync ops ─────────────────────────────────────────────────────────

def _op_create(org: str, principal: str, body: dict) -> tuple[int, dict]:
    avatar_key = str((body or {}).get("avatar_key")
                     or _default_avatar()).strip()
    meeting_ref = str((body or {}).get("meeting_ref") or "").strip()
    metadata = (body or {}).get("metadata")
    try:
        view = operator.create_session(
            org, principal=principal, avatar_key=avatar_key,
            meeting_ref=meeting_ref,
            metadata=metadata if isinstance(metadata, dict) else None,
        )
    except ProviderUnconfigured as exc:
        return 503, {"error": "browser_operator_unconfigured",
                     "detail": str(exc)[:160]}
    except operator.OwnershipError as exc:
        return 403, {"error": str(exc)[:120]}
    return 200, {"ok": True, "session": view}


def _op_set_metadata(org: str, principal: str, session_id: str,
                     body: dict) -> tuple[int, dict]:
    metadata = (body or {}).get("metadata")
    result = operator.set_metadata(
        org, session_id, metadata if isinstance(metadata, dict) else {},
        principal=principal)
    if result.get("reason") == "not_found":
        return 404, result
    if result.get("reason") == "not_owner":
        return 403, result
    return 200, result


def _default_avatar() -> str:
    from ..config import settings

    return settings.default_avatar_id or "laura"


def _op_get(org: str, principal: str, session_id: str) -> tuple[int, dict]:
    view = operator.get_session(org, session_id, principal=principal)
    if view is None:
        return 404, {"error": "unknown session"}
    return 200, {"session": view}


def _op_command(org: str, principal: str, session_id: str,
                body: dict) -> tuple[int, dict]:
    verb = str((body or {}).get("verb") or "").strip()
    expected = (body or {}).get("expected")
    result = operator.issue_command(
        org, session_id, verb=verb, principal=principal,
        command_id=str((body or {}).get("command_id") or ""),
        element_id=str((body or {}).get("element_id") or ""),
        url=str((body or {}).get("url") or ""),
        text=str((body or {}).get("text") or ""),
        direction=str((body or {}).get("direction") or "down"),
        verify=bool((body or {}).get("verify")),
        expected=expected if isinstance(expected, dict) else None,
    )
    if result.get("reason") == "not_found":
        return 404, result
    if str(result.get("reason", "")).startswith("invalid_state"):
        return 409, result
    return 200, result


def _op_observe(org: str, principal: str, session_id: str) -> tuple[int, dict]:
    result = operator.issue_command(
        org, session_id, verb="observe", principal=principal,
    )
    if result.get("reason") == "not_found":
        return 404, result
    if str(result.get("reason", "")).startswith("invalid_state"):
        return 409, result
    return 200, result


def _op_present(org: str, principal: str, session_id: str) -> tuple[int, dict]:
    result = operator.present(org, session_id, principal=principal)
    if not result.get("ok"):
        reason = result.get("reason")
        code = 404 if reason == "not_found" else (
            403 if reason == "not_owner" else 409)
        return code, result
    return 200, result


def _op_exchange(org: str, body: dict) -> tuple[int, dict]:
    token = str((body or {}).get("presentation_token") or "")
    result = operator.exchange_token(org, token)
    if not result.get("ok"):
        return 403, result
    return 200, result


def _op_close(org: str, principal: str, session_id: str) -> tuple[int, dict]:
    result = operator.close_session(org, session_id, principal=principal)
    return (403 if result.get("reason") == "not_owner" else 200), result


def _op_revoke(org: str, principal: str, session_id: str) -> tuple[int, dict]:
    result = operator.revoke_session(org, session_id, principal=principal)
    return (403 if result.get("reason") == "not_owner" else 200), result


# ── machine surface: /org/browser/* ─────────────────────────────────────────

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
    # Machine callers carry org authority, not a human principal.
    code, payload = await run_in_threadpool(handler, org, *args)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


@router.post("/org/browser/sessions")
async def org_browser_create(request: Request) -> JSONResponse:
    return await _machine(
        request, lambda org, body: _op_create(org, "", body), needs_body=True,
    )


@router.get("/org/browser/sessions/{sid}")
async def org_browser_get(sid: str, request: Request) -> JSONResponse:
    return await _machine(request, lambda org: _op_get(org, "", sid))


@router.post("/org/browser/sessions/{sid}/commands")
async def org_browser_command(sid: str, request: Request) -> JSONResponse:
    return await _machine(
        request, lambda org, body: _op_command(org, "", sid, body),
        needs_body=True,
    )


@router.get("/org/browser/sessions/{sid}/observation")
async def org_browser_observe(sid: str, request: Request) -> JSONResponse:
    return await _machine(request, lambda org: _op_observe(org, "", sid))


@router.post("/org/browser/sessions/{sid}/metadata")
async def org_browser_metadata(sid: str, request: Request) -> JSONResponse:
    return await _machine(
        request, lambda org, body: _op_set_metadata(org, "", sid, body),
        needs_body=True,
    )


@router.post("/org/browser/sessions/{sid}/present")
async def org_browser_present(sid: str, request: Request) -> JSONResponse:
    return await _machine(request, lambda org: _op_present(org, "", sid))


@router.post("/org/browser/present/exchange")
async def org_browser_exchange(request: Request) -> JSONResponse:
    return await _machine(
        request, lambda org, body: _op_exchange(org, body), needs_body=True,
    )


@router.post("/org/browser/sessions/{sid}/close")
async def org_browser_close(sid: str, request: Request) -> JSONResponse:
    return await _machine(request, lambda org: _op_close(org, "", sid))


@router.post("/org/browser/sessions/{sid}/revoke")
async def org_browser_revoke(sid: str, request: Request) -> JSONResponse:
    return await _machine(request, lambda org: _op_revoke(org, "", sid))


# ── dashboard twin (cookie + same-origin; strict method checks) ─────────────

@router.api_route("/dashboard/browser/{tail:path}",
                  methods=["GET", "POST"])
async def dashboard_browser(tail: str, request: Request) -> JSONResponse:
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
        if parts == ["sessions"] and method == "POST":
            return _op_create(org, principal, body)
        if len(parts) == 2 and parts[0] == "sessions" and method == "GET":
            return _op_get(org, principal, parts[1])
        if (len(parts) == 3 and parts[0] == "sessions"
                and parts[2] == "commands" and method == "POST"):
            return _op_command(org, principal, parts[1], body)
        if (len(parts) == 3 and parts[0] == "sessions"
                and parts[2] == "observation" and method == "GET"):
            return _op_observe(org, principal, parts[1])
        if (len(parts) == 3 and parts[0] == "sessions"
                and parts[2] == "metadata" and method == "POST"):
            return _op_set_metadata(org, principal, parts[1], body)
        if (len(parts) == 3 and parts[0] == "sessions"
                and parts[2] == "present" and method == "POST"):
            return _op_present(org, principal, parts[1])
        if parts == ["present", "exchange"] and method == "POST":
            return _op_exchange(org, body)
        if (len(parts) == 3 and parts[0] == "sessions"
                and parts[2] == "close" and method == "POST"):
            return _op_close(org, principal, parts[1])
        if (len(parts) == 3 and parts[0] == "sessions"
                and parts[2] == "revoke" and method == "POST"):
            return _op_revoke(org, principal, parts[1])
        return 404, {"error": "unknown browser route"}

    code, payload = await run_in_threadpool(run)
    return JSONResponse(payload, status_code=code, headers=_NO_STORE)


_ = dal  # DAL touched via operator; kept for module cohesion
