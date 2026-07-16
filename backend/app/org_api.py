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

import hmac as _hmac
import json
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import cedric, executor, ledger, store
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


# ─────────── the canonical approval door (handshake: approve-action) ───────────
# Agreed action-lifecycle contract hsk_con_cnw4567mqj3p49dyn3dg: EVERY approval
# — dashboard or a Slack decision relayed by the orchestrator — converges on
# this ONE idempotent transition. Cedric never executes on locally-held
# approval state; this door's 200 is the only execution trigger.

_TERMINAL = {"done", "rejected", "failed"}


def _global_bearer_used(request: Request) -> bool:
    """True when the caller presented the DEPLOYMENT-global bearer (valid only
    for the Demo org on this door — contract clause B1)."""
    token = settings.laura_api_token.strip()
    if not token:
        return False
    provided = request.headers.get("authorization", "")
    return _hmac.compare_digest(provided, f"Bearer {token}")


def _org_action(org: str, action_id: str) -> tuple[dict, str] | None:
    """The stored artifact action visible to ``org`` (same scan the dashboard
    approve door uses — the saved artifact is the trusted source for the typed
    spec, never the client body)."""
    from . import dashboard  # local import: dashboard imports nothing from here

    return dashboard._find_org_action(org, action_id)


def _unmet_dependencies(org: str, action: dict) -> list[str]:
    """Dependency gate [M8]: dependency action_ids not yet terminal-done."""
    deps = [str(x) for x in (action.get("dependencies") or []) if str(x).strip()]
    if not deps:
        return []
    statuses = ledger.action_statuses(deps, org_id=org)
    return [d for d in deps if (statuses.get(d) or {}).get("status") != "done"]


def _execute_route(org: str, action_id: str, action: dict) -> tuple[str | None, str]:
    """Run the approved action per its persisted execution_route [B2].
    Returns (execution_job_id | None, new_status)."""
    route = str(action.get("execution_route") or "").strip() or (
        # Legacy actions (pre routing-fields) derive the route the same way
        # finalize now stamps it: native iff the executor can run the typed spec.
        "native" if executor.enabled() and executor.from_typed(action.get("typed")) else "cedric"
    )
    if route == "cedric":
        # tenancy-v4 dispatch-action is the sanctioned path once THAT contract
        # is accepted; until then the approval stands recorded and the
        # orchestrator's own loop picks the action up from action.requested.
        return None, "approved"
    exec_action = executor.from_typed(action.get("typed"))
    if exec_action is None or not executor.handles(exec_action):
        return None, "approved"
    job_id = uuid.uuid4().hex
    # execute_approved writes its own done/failed receipt into the same
    # provenance channel the dashboard reads [contract: native-route surfacing].
    result = executor.execute_approved(org, action_id, exec_action)
    return job_id, ("done" if result.get("ok") else "failed")


@router.post("/actions/{action_id}/approve")
async def org_action_approve(action_id: str, request: Request) -> JSONResponse:
    """handshake operation: approve-action (B->A). Body: SlackApprovalEvent
    (decision required; laura_user_id, idempotency_key, selected_slot_id,
    decided_via, response_text optional per contract)."""
    # [B1] auth: per-org bearer resolves the tenant; the global deployment
    # bearer is valid ONLY for the Demo org — everywhere else 401.
    err, org = await _machine_gate(request)
    if err:
        return err
    header_org = (request.headers.get("x-laura-org-id") or "").strip()
    if _global_bearer_used(request) and header_org and header_org != settings.demo_org_id:
        return JSONResponse({"error": "org_token_required"}, status_code=401)
    # X-Laura-Org-Id is a cross-check, never a resolver: mismatch = 404,
    # indistinguishable from an unknown action by design.
    if header_org and header_org != org:
        return JSONResponse({"error": "unknown action for this org"}, status_code=404)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    body = body if isinstance(body, dict) else {}
    decision = str(body.get("decision") or "").strip().lower()
    if decision not in ("approve", "reject", "respond"):
        return JSONResponse(
            {"error": "decision must be approve|reject|respond"}, status_code=400
        )
    idem = str(body.get("idempotency_key") or "").strip()
    slot = str(body.get("selected_slot_id") or "").strip()
    laura_user = str(body.get("laura_user_id") or "").strip()
    via = str(body.get("decided_via") or "slack").strip().lower()

    found = await run_in_threadpool(_org_action, org, action_id)
    if found is None:
        return JSONResponse({"error": "unknown action for this org"}, status_code=404)
    action, _avatar = found

    # Approver rule: approver_user_ids present -> only those; absent -> any
    # member of the org (the dashboard-door rule).
    approvers = [str(x) for x in (action.get("approver_user_ids") or [])]
    if approvers and laura_user not in approvers:
        return JSONResponse({"error": "approver_not_allowed"}, status_code=403)
    if not approvers and laura_user:
        member = await run_in_threadpool(store.is_org_member, laura_user, org)
        if not member:
            return JSONResponse({"error": "approver_not_in_org"}, status_code=403)

    # [M1] decision-based convergence: answer replays/conflicts from the ONE
    # recorded decision, never from the idempotency key alone.
    recorded = await run_in_threadpool(store.get_action_approval, org, action_id)
    if recorded is not None:
        # Same decision (and same slot where applicable) — regardless of
        # idempotency_key — replays the recorded result [M1 case a+b].
        same = (
            recorded["decision"] == decision
            and (recorded["selected_slot_id"] or "") == slot
        )
        if same:
            return JSONResponse({
                "ok": True, "action_id": action_id, "idempotent_replay": True,
                "previous_status": recorded["previous_status"],
                "new_status": recorded["new_status"],
                "execution_job_id": recorded["execution_job_id"],
            })
        return JSONResponse({
            "error": "decision_conflict", "action_id": action_id,
            "current_status": recorded["new_status"],
            "decided_via": recorded["decided_via"],
            "decided_at": recorded["decided_at"],
        }, status_code=409)

    prev = ((await run_in_threadpool(
        ledger.action_statuses, [action_id], org_id=org
    )).get(action_id) or {}).get("status") or "proposed"
    if prev in _TERMINAL:
        # No recorded approval but a terminal status (e.g. auto-push already
        # ran it): treat matching intent as replay-of-outcome, else conflict.
        if decision == "approve" and prev == "done":
            return JSONResponse({
                "ok": True, "action_id": action_id, "idempotent_replay": True,
                "previous_status": prev, "new_status": prev, "execution_job_id": None,
            })
        return JSONResponse({
            "error": "decision_conflict", "action_id": action_id,
            "current_status": prev, "decided_via": "system", "decided_at": None,
        }, status_code=409)

    # [M9] slot rules — only when the action carries a proposal.
    proposal = action.get("proposal") if isinstance(action.get("proposal"), dict) else None
    if decision == "approve" and proposal:
        slots = {str(s.get("slot_id")): s for s in (proposal.get("candidate_slots") or [])
                 if isinstance(s, dict)}
        if slot not in slots:
            return JSONResponse(
                {"error": "unknown selected_slot_id", "action_id": action_id},
                status_code=422,
            )
        chosen = slots[slot]
        try:
            stale = str(chosen.get("start") or "") <= time.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:  # noqa: BLE001
            stale = False
        if stale:
            return JSONResponse(
                {"error": "slot_stale", "slot_id": slot}, status_code=409
            )
        # Materialize the chosen slot into the typed spec before execution.
        typed = action.get("typed") if isinstance(action.get("typed"), dict) else {}
        args = dict(typed.get("args") or {})
        args["start"], args["end"] = chosen.get("start"), chosen.get("end")
        action = {**action, "typed": {**typed, "args": args}}

    # ── the canonical transition ──
    if decision == "reject":
        await run_in_threadpool(
            ledger.set_action_status, action_id, "rejected",
            f"rejected via {via}", org_id=org,
        )
        new_status, job_id, blocked = "rejected", None, []
    elif decision == "respond":
        detail = ("response: " + str(body.get("response_text") or "").strip())[:300]
        await run_in_threadpool(
            ledger.set_action_status, action_id, "done", detail, org_id=org
        )
        new_status, job_id, blocked = "done", None, []
    else:  # approve
        await run_in_threadpool(
            ledger.set_action_status, action_id, "approved",
            f"approved via {via}" + (f" by {laura_user}" if laura_user else ""),
            org_id=org,
        )
        blocked = await run_in_threadpool(_unmet_dependencies, org, action)
        if blocked:
            new_status, job_id = "approved", None  # [M8] executes when deps land
        else:
            job_id, new_status = await run_in_threadpool(
                _execute_route, org, action_id, action
            )

    await run_in_threadpool(
        lambda: store.record_action_approval(
            org, action_id, decision=decision, selected_slot_id=slot,
            idempotency_key=idem, decided_via=via, laura_user_id=laura_user,
            previous_status=prev, new_status=new_status,
            execution_job_id=job_id, blocked_on=json.dumps(blocked),
        )
    )
    resp: dict = {
        "ok": True, "action_id": action_id, "idempotent_replay": False,
        "previous_status": prev, "new_status": new_status,
        "execution_job_id": job_id,
    }
    if blocked:
        resp["blocked_on"] = blocked
    return JSONResponse(resp)
