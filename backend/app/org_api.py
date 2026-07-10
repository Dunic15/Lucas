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

import json

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


@router.post("/actions/{ref}/resolve")
async def org_resolve(ref: str, request: Request) -> JSONResponse:
    """Close a ledger item from the outside (e.g. ticked off in Slack).

    ``ref`` is either the numeric ledger row id (as before) OR the stable
    string ``action_id`` the orchestrator carries from action.requested /
    session.ended — the natural key for Cedric's ack loop, since it never sees
    the numeric row id.

    OPTIONAL JSON body (agreed contract — orchestrator side implements in
    parallel): {"outcome": "done"|"rejected"|"failed", "detail": "<=300 chars"}.
    Absent/empty body means "done", so today's body-less callers keep working
    identically. All three outcomes are terminal; the response echoes the
    status actually applied."""
    if err := cedric.auth_error(request):
        return err
    raw = await request.body()
    if raw.strip():
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if body is not None and not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    else:
        body = None  # no body at all — the pre-contract client shape
    outcome = str((body or {}).get("outcome") or "done").strip().lower()
    if outcome not in ledger.RESOLUTION_OUTCOMES:
        return JSONResponse(
            {"error": f"outcome must be one of {list(ledger.RESOLUTION_OUTCOMES)}"},
            status_code=400,
        )
    detail = str((body or {}).get("detail") or "").strip()[:300]
    if ref.isdigit():
        ok = await run_in_threadpool(ledger.resolve_item, int(ref), "", outcome, detail)
    else:
        ok = await run_in_threadpool(ledger.resolve_by_action_id, ref, "", outcome, detail)
    if not ok:
        return JSONResponse({"error": "unknown or already resolved item"}, status_code=404)
    return JSONResponse({"resolved": True, "id": ref, "status": outcome})


@router.post("/actions/{action_id}/status")
async def org_action_status(action_id: str, request: Request) -> JSONResponse:
    """Execution provenance from the orchestrator: where an action stands on
    the brain's side (proposed → approved/rejected → done/failed), keyed on the
    stable action_id it received in action.requested / session.ended. Upsert,
    latest wins; 'done' also closes the ledger item (same as /resolve). The
    dashboard shows this per action — the meter of 'my avatar's asks actually
    got executed'. Body: {"status": "...", "detail": "one-liner, optional"}."""
    if err := cedric.auth_error(request):
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a client error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    status = str((body or {}).get("status") or "")
    detail = str((body or {}).get("detail") or "")
    ok = await run_in_threadpool(ledger.set_action_status, action_id, status, detail)
    if not ok:
        return JSONResponse(
            {"error": f"status must be one of {list(ledger.EXECUTION_STATUSES)}"},
            status_code=400,
        )
    return JSONResponse({"recorded": True, "action_id": action_id, "status": status.lower()})
