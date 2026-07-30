"""Meeting Memory admin surface — /org/memory/* (machine door).

The owner-ratified visibility rule (spec §7) is all-attendees by default;
these two endpoints are the ratified escape hatches: an org admin may tag one
meeting org-public, or grant one named person access to one meeting. Both are
per-meeting and additive — there is deliberately NO person-wide or org-wide
grant here. Every route 404s when the feature flag is off (key-free demo and
existing deployments never see the surface), and sits behind the same per-org
bearer gate as the rest of /org/*.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import meeting_memory

router = APIRouter(tags=["meeting-memory"])

_NO_STORE = {"Cache-Control": "no-store"}


def _disabled() -> JSONResponse:
    return JSONResponse(
        {"error": "meeting_memory_disabled"}, status_code=404,
        headers=_NO_STORE,
    )


async def _org_gate(request: Request):
    from .. import org_api

    return await org_api._machine_gate(request)


@router.post("/org/memory/grant")
async def org_memory_grant(request: Request) -> JSONResponse:
    if not meeting_memory.enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await run_in_threadpool(
        meeting_memory.grant,
        org,
        str((body or {}).get("bot_id") or ""),
        str((body or {}).get("person") or ""),
        str((body or {}).get("granted_by") or ""),
    )
    return JSONResponse(
        res, status_code=200 if res.get("ok") else 400, headers=_NO_STORE
    )


@router.post("/org/memory/visibility")
async def org_memory_visibility(request: Request) -> JSONResponse:
    if not meeting_memory.enabled():
        return _disabled()
    err, org = await _org_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await run_in_threadpool(
        meeting_memory.set_visibility,
        org,
        str((body or {}).get("bot_id") or ""),
        str((body or {}).get("visibility") or ""),
    )
    return JSONResponse(
        res, status_code=200 if res.get("ok") else 400, headers=_NO_STORE
    )
