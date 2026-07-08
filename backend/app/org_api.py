"""Org-memory API — the read/act seam for surfaces (issue #48).

Surfaces (Cedric in Slack is the first) query what the avatars learned across
meetings: the carryover brief, open action items, and resolving them from the
outside ("done via Slack"). Everything here is DISTILLED data — briefs and
ledger items, never transcripts — and every route sits behind the same Bearer
gate as the session API (open when LAURA_API_TOKEN is unset, for local dev).

Kept as its own router so main.py stays a 2-line include; the functions are
thin wrappers over ledger.py, which remains the single source of truth.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import cedric, ledger

router = APIRouter(prefix="/org", tags=["org-memory"])


@router.get("/brief")
async def org_brief(meeting_url: str, request: Request) -> JSONResponse:
    """The carryover brief for a meeting link: what previous sessions left
    open (process steps, actions with owners, recent decisions)."""
    if err := cedric.auth_error(request):
        return err
    brief = await run_in_threadpool(ledger.carryover_brief, meeting_url)
    return JSONResponse(
        {"meeting_key": ledger.meeting_key(meeting_url), "brief": brief}
    )


@router.get("/actions")
async def org_actions(request: Request) -> JSONResponse:
    """Open ledger items across all meetings, grouped by meeting key."""
    if err := cedric.auth_error(request):
        return err
    grouped = await run_in_threadpool(ledger.open_by_meeting)
    return JSONResponse({"open": grouped})


@router.post("/actions/{item_id}/resolve")
async def org_resolve(item_id: int, request: Request) -> JSONResponse:
    """Close a ledger item from the outside (e.g. ticked off in Slack)."""
    if err := cedric.auth_error(request):
        return err
    ok = await run_in_threadpool(ledger.resolve_item, item_id)
    if not ok:
        return JSONResponse({"error": "unknown or already resolved item"}, status_code=404)
    return JSONResponse({"resolved": True, "id": item_id})
