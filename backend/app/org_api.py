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
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import cedric, ledger, store
from .config import settings

router = APIRouter(prefix="/org", tags=["org-memory"])


async def _machine_gate(request: Request) -> tuple[Optional[JSONResponse], str]:
    """Authenticate the machine caller and resolve the tenant whose memory it
    may touch: a PER-ORG bearer (org_tokens, PR A/D) → its own org; the global
    bearer and the key-free open demo → the Demo org (exactly today's scope).
    Returns ``(error_response, org_id)`` — send the error when it is not None.
    The resolver is sync DB I/O, so it runs in the threadpool (every route
    here is async; never the live hot path)."""
    org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if org is None:
        if err := cedric.auth_error(request):
            return err, ""
        org = settings.demo_org_id  # key-free/open: today's demo scope
    return None, org


def _search_artifacts(query: str, limit: int, org_id: str) -> list[dict]:
    """Meetings whose summary/decisions/actions mention the query — a distilled
    snippet per hit, newest first, scoped to one org. No transcripts (they
    aren't in list_artifacts output beyond the distilled fields we read here)."""
    q = query.lower()
    hits: list[dict] = []
    for row in store.list_artifacts(org_id):
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
    err, org = await _machine_gate(request)
    if err:
        return err
    brief = await run_in_threadpool(ledger.carryover_brief, meeting_url, org_id=org)
    return JSONResponse(
        {"meeting_key": ledger.meeting_key(meeting_url), "brief": brief}
    )


@router.get("/actions")
async def org_actions(request: Request) -> JSONResponse:
    """Open ledger items across all meetings, grouped by meeting key."""
    err, org = await _machine_gate(request)
    if err:
        return err
    grouped = await run_in_threadpool(ledger.open_by_meeting, org_id=org)
    return JSONResponse({"open": grouped})


@router.get("/search")
async def org_search(q: str, request: Request, limit: int = 20) -> JSONResponse:
    """Ask across every meeting: 'what did we decide/commit about <q>?'. Returns
    matching ledger items (actions/decisions with owners + status) and matching
    meeting artifacts (a distilled snippet each). The 'employee that remembers'
    query — distilled data only, same Bearer gate."""
    err, org = await _machine_gate(request)
    if err:
        return err
    query = (q or "").strip()
    if not query:
        return JSONResponse({"error": "missing query ?q="}, status_code=400)
    limit = max(1, min(limit, 50))
    ledger_hits = await run_in_threadpool(ledger.search, query, limit=limit, org_id=org)
    meeting_hits = await run_in_threadpool(_search_artifacts, query, limit, org)
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
    err, org = await _machine_gate(request)
    if err:
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
        ok = await run_in_threadpool(
            ledger.resolve_item, int(ref), "", outcome, detail, org_id=org
        )
    else:
        ok = await run_in_threadpool(
            ledger.resolve_by_action_id, ref, "", outcome, detail, org_id=org
        )
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
    err, org = await _machine_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON is a client error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    status = str((body or {}).get("status") or "").strip().lower()
    detail = str((body or {}).get("detail") or "")
    # Validate the status VALUE here so a genuinely bad state is still a 400 —
    # distinct from "valid state, but this org has no durable action row yet".
    if status not in ledger.EXECUTION_STATUSES:
        return JSONResponse(
            {"error": f"status must be one of {list(ledger.EXECUTION_STATUSES)}"},
            status_code=400,
        )
    # action_status is keyed by (org_id, action_id), so a per-workspace
    # principal may safely report proposed/approved before meeting finalization.
    # The eventual ledger row consults only this org's status and cannot collide
    # with an identical action_id in another tenant.
    ok = await run_in_threadpool(
        ledger.set_action_status, action_id, status, detail, org_id=org
    )
    # Orchestrated mode: Cedric owns the action id. When the control-plane path
    # has no queued_actions row for (org, action_id) it returns False — a clean
    # no-op (nothing to decorate on the dashboard), NOT a client error. Return
    # recorded=false + 200 so Cedric's best-effort provenance loop stops getting
    # 400s (and stops falling back to /resolve, which 404s).
    return JSONResponse(
        {"recorded": bool(ok), "action_id": action_id, "status": status}
    )


@router.post("/actions/{action_id}/approve")
async def org_approve(action_id: str, request: Request) -> JSONResponse:
    """The Slack-approval -> EXECUTION bridge (agreed action contract, aura v2).

    ``/resolve`` and ``/status`` only RECORD an outcome; neither runs the
    executor — which is why clicking Approve in Slack changed the message but
    did nothing. This door closes that gap: a machine caller (Cedric, after it
    signature-verifies the Slack interaction and resolves the Slack user to a
    Laura user) POSTs here and Laura runs the SAME canonical transition + native
    execution the dashboard's approve door runs, on the tool owner's own
    credentials. Slack and dashboard now converge on one execution engine.

    Machine-gated (per-org bearer -> its org; Demo scope for the open key-free
    path). DECISION-BASED IDEMPOTENCY (contract M1): a repeat of the SAME
    decision replays the recorded result and NEVER executes twice; a conflicting
    decision after a terminal state is 409 ``decision_conflict``. The trusted
    typed spec comes from the SAVED artifact, never the request body. Body
    (all optional): {"decision":"approve"|"reject", "detail":str,
    "idempotency_key":str, "selected_slot_id":str}; absent decision = approve.

    Race note: the guard is the ledger's monotonic terminal status, which dedups
    SEQUENTIAL approvals (the real-world case). A truly simultaneous
    Slack+dashboard double-click within the same instant would need an atomic
    claim primitive — tracked as a follow-up; it never downgrades a terminal
    state, so at worst it is a rare double-execute, never a lost approval."""
    err, org = await _machine_gate(request)
    if err:
        return err
    aid = (action_id or "").strip()
    if not aid:
        return JSONResponse({"error": "action_id is required"}, status_code=400)
    raw = await request.body()
    body: dict = {}
    if raw.strip():
        try:
            body = json.loads(raw) or {}
        except ValueError:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    decision = str(body.get("decision") or "approve").strip().lower()
    if decision not in ("approve", "reject"):
        return JSONResponse(
            {"error": "decision must be approve|reject"}, status_code=400
        )
    detail = str(body.get("detail") or "").strip()[:300]

    # Canonical status is the idempotency basis. Terminal states (done/rejected/
    # failed) are monotonic in the ledger, so a replay can't repaint them.
    current = await run_in_threadpool(ledger.action_statuses, [aid], org_id=org)
    cur = (current.get(aid) or {}).get("status") or ""

    if decision == "reject":
        if cur == "done":
            return JSONResponse(
                {"error": "decision_conflict", "action_id": aid,
                 "current_status": cur, "decided_via": "slack"}, status_code=409)
        replay = cur == "rejected"
        if not replay:
            await run_in_threadpool(
                ledger.set_action_status, aid, "rejected",
                detail or "rejected via Slack", org_id=org)
        return JSONResponse(
            {"ok": True, "action_id": aid, "decision": "reject",
             "new_status": "rejected", "idempotent_replay": replay})

    # decision == approve
    if cur == "rejected":
        return JSONResponse(
            {"error": "decision_conflict", "action_id": aid,
             "current_status": cur, "decided_via": "slack"}, status_code=409)
    if cur in ("approved", "done"):
        # Already approved (and maybe executed) — replay, NEVER execute again.
        return JSONResponse(
            {"ok": True, "action_id": aid, "decision": "approve",
             "new_status": cur, "executed": cur == "done",
             "idempotent_replay": True, "status": current.get(aid)})

    # Fresh approve — load the trusted typed spec from the saved artifact and run
    # the SAME executor path as the dashboard door. Lazy import: dashboard owns
    # the artifact-scoped loader + the typed->executor bridge, so both approval
    # doors stay in lockstep.
    from . import executor
    from .dashboard import _executor_action, _find_org_action

    found = await run_in_threadpool(_find_org_action, org, aid)
    if found is None:
        return JSONResponse(
            {"error": "unknown action for this org"}, status_code=404)
    action, acting_avatar = found

    await run_in_threadpool(
        ledger.set_action_status, aid, "approved",
        detail or "approved via Slack", org_id=org)

    typed = action.get("typed") if isinstance(action.get("typed"), dict) else None
    exec_action = _executor_action(typed)
    executed = False
    capability_blocked = False
    if exec_action is not None and executor.handles(exec_action):
        caps = await run_in_threadpool(store.get_avatar_capabilities, acting_avatar)
        if caps.get(executor.capability_family(exec_action.get("type"))) is False:
            capability_blocked = True
        else:
            # execute_approved writes its own done/failed receipt to the ledger.
            await run_in_threadpool(executor.execute_approved, org, aid, exec_action)
            executed = True

    latest = await run_in_threadpool(ledger.action_statuses, [aid], org_id=org)
    return JSONResponse(
        {"ok": True, "action_id": aid, "decision": "approve", "approved": True,
         "executed": executed, "capability_blocked": capability_blocked,
         "typed": bool(typed), "idempotent_replay": False,
         "status": latest.get(aid)})
