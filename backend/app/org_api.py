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

from . import cedric, ledger, store

router = APIRouter(prefix="/org", tags=["org-memory"])


def _search_artifacts(query: str, limit: int) -> list[dict]:
    """Meetings whose summary/decisions/actions mention the query — a distilled
    snippet per hit, newest first. No transcripts (they aren't in list_artifacts
    output beyond the distilled fields we read here)."""
    q = query.lower()
    hits: list[dict] = []
    for row in store.list_artifacts():
        art = row.get("artifact") or {}
        hay = [art.get("summary", "")]
        hay += [str(d) for d in (art.get("decisions") or [])]
        for a in art.get("actions") or []:
            hay.append(a.get("item", "") if isinstance(a, dict) else str(a))
        matches = [h for h in hay if h and q in h.lower()]
        if matches:
            hits.append(
                {
                    "bot_id": row.get("bot_id"),
                    "saved_at": row.get("saved_at"),
                    "meeting_type": art.get("meeting_type", ""),
                    "snippet": matches[0][:240],
                }
            )
        if len(hits) >= limit:
            break
    return hits


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


@router.get("/search")
async def org_search(q: str, request: Request, limit: int = 20) -> JSONResponse:
    """Ask across every meeting: 'what did we decide/commit about <q>?'. Returns
    matching ledger items (actions/decisions with owners + status) and matching
    meeting artifacts (a distilled snippet each). The 'employee that remembers'
    query — distilled data only, same Bearer gate."""
    if err := cedric.auth_error(request):
        return err
    query = (q or "").strip()
    if not query:
        return JSONResponse({"error": "missing query ?q="}, status_code=400)
    limit = max(1, min(limit, 50))
    ledger_hits = await run_in_threadpool(ledger.search, query, limit=limit)
    meeting_hits = await run_in_threadpool(_search_artifacts, query, limit)
    return JSONResponse(
        {"query": query, "ledger_matches": ledger_hits, "meeting_matches": meeting_hits}
    )


@router.post("/actions/{item_id}/resolve")
async def org_resolve(item_id: int, request: Request) -> JSONResponse:
    """Close a ledger item from the outside (e.g. ticked off in Slack)."""
    if err := cedric.auth_error(request):
        return err
    ok = await run_in_threadpool(ledger.resolve_item, item_id)
    if not ok:
        return JSONResponse({"error": "unknown or already resolved item"}, status_code=404)
    return JSONResponse({"resolved": True, "id": item_id})
