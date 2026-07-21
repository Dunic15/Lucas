"""Org-memory API; the read/act seam for surfaces (issue #48).

Surfaces (Cedric in Slack is the first) query what the avatars learned across
meetings: the carryover brief, open action items, and resolving them from the
outside ("done via Slack"). Everything here is DISTILLED data; briefs and
ledger items, never transcripts; and every route sits behind the same Bearer
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

from .. import action_plane, cedric, executor, ledger, pipedream_executor, store
from ..config import settings

router = APIRouter(prefix="/org", tags=["org-memory"])


async def _machine_gate(request: Request) -> tuple[Optional[JSONResponse], str]:
    """Authenticate the machine caller and resolve the tenant whose memory it
    may touch: a PER-ORG bearer (org_tokens, PR A/D) → its own org; the global
    bearer and the key-free open demo → the Demo org (exactly today's scope).
    Returns ``(error_response, org_id)``: send the error when it is not None.
    The resolver is sync DB I/O, so it runs in the threadpool (every route
    here is async; never the live hot path)."""
    org = await run_in_threadpool(cedric.resolve_machine_org, request)
    if org is None:
        if err := cedric.auth_error(request):
            return err, ""
        org = settings.demo_org_id  # key-free/open: today's demo scope
    return None, org


def _search_artifacts(query: str, limit: int, org_id: str) -> list[dict]:
    """Meetings whose summary/decisions/actions mention the query; a distilled
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
    session.ended; the natural key for Cedric's ack loop, since it never sees
    the numeric row id.

    OPTIONAL JSON body (agreed contract; orchestrator side implements in
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
        body = None  # no body at all; the pre-contract client shape
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
    dashboard shows this per action; the meter of 'my avatar's asks actually
    got executed'. Body: {"status": "...", "detail": "one-liner, optional"}."""
    err, org = await _machine_gate(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001; malformed JSON is a client error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    # Boundary normalization (canonical Action plane): peers historically say
    # 'executed' for a completed run; canonical vocabulary says 'done'. The
    # alias map is inbound-only; Laura never emits the alias.
    status = action_plane.normalize_status(str((body or {}).get("status") or ""))
    detail = str((body or {}).get("detail") or "")
    # Validate the status VALUE here so a genuinely bad state is still a 400 -
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
    # has no queued_actions row for (org, action_id) it returns False; a clean
    # no-op (nothing to decorate on the dashboard), NOT a client error. Return
    # recorded=false + 200 so Cedric's best-effort provenance loop stops getting
    # 400s (and stops falling back to /resolve, which 404s).
    return JSONResponse(
        {"recorded": bool(ok), "action_id": action_id, "status": status}
    )


# ─────────── the canonical approval door (handshake: approve-action) ───────────
# Agreed action-lifecycle contract hsk_con_cnw4567mqj3p49dyn3dg: EVERY approval
#, dashboard or a Slack decision relayed by the orchestrator, converges on
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
    approve door uses; the saved artifact is the trusted source for the typed
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


def _execute_route(
    org: str, action_id: str, action: dict, acting_avatar: str = "",
    *, idempotency_key: str = "", via: str = "",
) -> tuple[str | None, str, bool]:
    """Run the approved action per its persisted execution_route [B2].
    Returns (execution_job_id | None, new_status, capability_blocked)."""
    route = str(action.get("execution_route") or "").strip() or (
        # Legacy actions (pre routing-fields) derive the route the same per-family
        # way finalize now stamps it (Pipedream for Asana + long tail when on,
        # else native for Google/Slack, else Cedric).
        executor.route_for_typed(action.get("typed"))
    )
    if route == "cedric":
        # handshake B2: hand the approved action to Cedric for execution
        # through its connectors (dispatch-action, pre_approved). Terminal
        # status comes BACK through the /status door when Cedric executes;
        # until then the canonical state is approved. Soft: a missing B-side
        # receiver leaves the action approved for the legacy pickup loop.
        from ..cedric import callback as cedric_callback

        cedric_callback.dispatch_action(org, action)
        return None, "approved", False
    if route == "browser":
        # Guarded browser step (B0): claim exactly-once, then re-check
        # ownership/state/avatar allowance and settle a receipt through the
        # browser operator; the SAME claim + provenance path, no second
        # execution system.
        from .. import browser

        if not browser.enabled():
            return None, "approved", False
        if not ledger.claim_action_execution(
            action_id, org_id=org, idempotency_key=idempotency_key, via=via
        ):
            latest = (ledger.action_statuses([action_id], org_id=org)
                      .get(action_id) or {})
            return None, str(latest.get("status") or "approved"), False
        from ..browser import operator as browser_operator

        result = browser_operator.execute_approved_step(org, action_id, action)
        return (uuid.uuid4().hex,
                "done" if result.get("ok") else "failed", False)
    if route == "pipedream":
        # Pipedream Connect-Proxy execution (Asana + long tail). Same capability
        # gate + exactly-once claim + provenance channel as the native route -
        # only the vendor call differs. Always returns; never falls through to
        # native (which could double-run the same action type).
        exec_action = executor.from_typed(action.get("typed"))
        if exec_action is None or not pipedream_executor.handles(exec_action):
            return None, "approved", False
        family = executor.capability_family(exec_action.get("type"))
        caps = store.get_avatar_capabilities(acting_avatar)
        if caps.get(family) is False:
            return None, "approved", True
        from .. import avatar_resolver

        if not avatar_resolver.family_allowed(org, acting_avatar, family):
            return None, "approved", True
        if not ledger.claim_action_execution(
            action_id, org_id=org, idempotency_key=idempotency_key, via=via
        ):
            latest = (ledger.action_statuses([action_id], org_id=org)
                      .get(action_id) or {})
            return None, str(latest.get("status") or "approved"), False
        job_id = uuid.uuid4().hex
        if settings.action_dispatch_async:
            import threading

            def _dispatch_pd() -> None:
                try:
                    result = pipedream_executor.execute_approved(
                        org, action_id, exec_action
                    )
                    ledger.set_action_decision_result(
                        action_id, org_id=org,
                        new_status="done" if result.get("ok") else "failed",
                        execution_job_id=job_id,
                    )
                except Exception:  # noqa: BLE001; executor soft-returns
                    pass

            threading.Thread(target=_dispatch_pd, daemon=True).start()
            return job_id, "executing", False
        result = pipedream_executor.execute_approved(org, action_id, exec_action)
        return job_id, ("done" if result.get("ok") else "failed"), False
    exec_action = executor.from_typed(action.get("typed"))
    if exec_action is None or not executor.handles(exec_action):
        return None, "approved", False
    # CAPABILITY GATE (same rule as the dashboard door, from #255): the acting
    # avatar's family toggle can veto native execution; a blocked action stays
    # `approved`: byte-identical to the executor being off. The M2 overlay
    # check is the org-scoped narrowing re-resolved at EXECUTION time (an
    # overlay can remove a capability, never grant one; flag off ⇒ allowed).
    family = executor.capability_family(exec_action.get("type"))
    caps = store.get_avatar_capabilities(acting_avatar)
    if caps.get(family) is False:
        return None, "approved", True
    from .. import avatar_resolver

    if not avatar_resolver.family_allowed(org, acting_avatar, family):
        return None, "approved", True
    # EXECUTION CLAIM (canonical Action plane, M0): the atomic CAS to
    # 'executing' is the only license to call a vendor. Losing the claim means
    # another surface/instance is executing (or already finished) this exact
    # action; report its status instead of writing twice.
    if not ledger.claim_action_execution(
        action_id, org_id=org, idempotency_key=idempotency_key, via=via
    ):
        latest = (ledger.action_statuses([action_id], org_id=org)
                  .get(action_id) or {})
        return None, str(latest.get("status") or "approved"), False
    job_id = uuid.uuid4().hex
    if settings.action_dispatch_async:
        # Observable async dispatch: the door answers 'executing' immediately;
        # the executor settles done/failed through the same provenance channel
        # (and mirrors it to Cedric) from a worker thread.
        import threading

        def _dispatch() -> None:
            try:
                result = executor.execute_approved(org, action_id, exec_action)
                ledger.set_action_decision_result(
                    action_id, org_id=org,
                    new_status="done" if result.get("ok") else "failed",
                    execution_job_id=job_id,
                )
            except Exception:  # noqa: BLE001; executor soft-returns; belt+braces
                pass

        threading.Thread(target=_dispatch, daemon=True).start()
        return job_id, "executing", False
    # execute_approved writes its own done/failed receipt into the same
    # provenance channel the dashboard reads [contract: native-route surfacing].
    result = executor.execute_approved(org, action_id, exec_action)
    return job_id, ("done" if result.get("ok") else "failed"), False


@router.post("/actions/{action_id}/approve")
async def org_action_approve(action_id: str, request: Request) -> JSONResponse:
    """handshake operation: approve-action (B->A). Body: SlackApprovalEvent
    (decision required; laura_user_id, idempotency_key, selected_slot_id,
    decided_via, response_text optional per contract)."""
    # [B1] auth: per-org bearer resolves the tenant; the global deployment
    # bearer is valid ONLY for the Demo org; everywhere else 401.
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
    # Absent decision defaults to approve (#255's lenient relay compatibility);
    # an EXPLICIT unknown value is still a client error.
    decision = str(body.get("decision") or "approve").strip().lower()
    if decision not in ("approve", "reject", "respond"):
        return JSONResponse(
            {"error": "decision must be approve|reject|respond"}, status_code=400
        )
    extra_detail = str(body.get("detail") or "").strip()[:200]
    idem = str(body.get("idempotency_key") or "").strip()
    slot = str(body.get("selected_slot_id") or "").strip()
    laura_user = str(body.get("laura_user_id") or "").strip()
    via = str(body.get("decided_via") or "slack").strip().lower()

    found = await run_in_threadpool(_org_action, org, action_id)
    if found is None:
        return JSONResponse({"error": "unknown action for this org"}, status_code=404)
    action, acting_avatar = found
    # Edited params win over the artifact's original spec (canonical Action
    # plane): the params door validated them against the schema, never a
    # client body on THIS door.
    effective = await run_in_threadpool(
        lambda: ledger.effective_typed(action_id, action.get("typed"), org_id=org)
    )
    if effective is not None:
        action = {**action, "typed": effective}

    # Approver rule: approver_user_ids present -> only those; absent -> any
    # member of the org (the dashboard-door rule).
    approvers = [str(x) for x in (action.get("approver_user_ids") or [])]
    if approvers and laura_user not in approvers:
        return JSONResponse({"error": "approver_not_allowed"}, status_code=403)
    if not approvers and laura_user:
        member = await run_in_threadpool(store.is_org_member, laura_user, org)
        if not member:
            return JSONResponse({"error": "approver_not_in_org"}, status_code=403)

    def _replay_response(recorded: dict) -> JSONResponse:
        """[M1] answer replays/conflicts from the ONE recorded decision, never
        from the idempotency key alone. Same decision (and same slot where
        applicable) replays the recorded result; anything else is a 409."""
        same = (
            recorded["decision"] == decision
            and (recorded["selected_slot_id"] or "") == slot
        )
        if same:
            return JSONResponse({
                "ok": True, "action_id": action_id, "idempotent_replay": True,
                "approved": recorded["decision"] == "approve",
                "previous_status": recorded["previous_status"],
                "new_status": recorded["new_status"],
                "execution_job_id": recorded["execution_job_id"],
                "executed": recorded["new_status"] in ("done", "failed")
                and bool(recorded["execution_job_id"]),
            })
        return JSONResponse({
            "error": "decision_conflict", "action_id": action_id,
            "current_status": recorded["new_status"],
            "decided_via": recorded["decided_via"],
            "decided_at": recorded["decided_at"],
        }, status_code=409)

    recorded = await run_in_threadpool(
        lambda: ledger.get_action_decision(action_id, org_id=org)
    )
    if recorded is not None:
        return _replay_response(recorded)

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

    # [M9] slot rules; only when the action carries a proposal.
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

    # needs_details gate (AFTER slot materialization; a proposal's start/end
    # legitimately arrive from the chosen slot): a typed spec still missing
    # REQUIRED parameters must not be approved into a broken vendor call or a
    # silent no-op; surface the exact fields so either surface can collect
    # them through the params door.
    typed_now = action.get("typed") if isinstance(action.get("typed"), dict) else None
    missing = action_plane.missing_params(typed_now)
    if decision == "approve" and typed_now and missing:
        await run_in_threadpool(
            ledger.set_action_status, action_id, "needs_details",
            "missing: " + ", ".join(missing), org_id=org,
        )
        return JSONResponse({
            "error": "needs_details", "action_id": action_id,
            "missing_params": missing,
            "params_schema": action_plane.params_schema(typed_now),
        }, status_code=422)

    # Dependencies are read-only; resolve them BEFORE recording so the
    # decision row carries the real blocked_on list from the start.
    blocked = (
        await run_in_threadpool(_unmet_dependencies, org, action)
        if decision == "approve" else []
    )

    # ── record THE decision first (two-phase canonical approve) ──
    # First write wins across instances and surfaces; the loser answers from
    # the winner's row exactly like a replay. Execution happens only behind
    # the recorded decision + the execution claim in _execute_route.
    prelim = {"approve": "approved", "reject": "rejected", "respond": "done"}[decision]
    recorded_now = await run_in_threadpool(
        lambda: ledger.record_action_decision(
            action_id, org_id=org, decision=decision, selected_slot_id=slot,
            idempotency_key=idem, decided_via=via, laura_user_id=laura_user,
            previous_status=prev, new_status=prelim,
            execution_job_id=None, blocked_on=json.dumps(blocked),
        )
    )
    if not recorded_now:
        recorded = await run_in_threadpool(
            lambda: ledger.get_action_decision(action_id, org_id=org)
        )
        if recorded is not None:
            return _replay_response(recorded)
        # Recording failed without a visible winner (storage hiccup): refuse
        # rather than execute outside the canonical record.
        return JSONResponse(
            {"error": "decision_not_recorded", "action_id": action_id},
            status_code=503,
        )

    # ── the canonical transition ──
    capability_blocked = False
    if decision == "reject":
        await run_in_threadpool(
            ledger.set_action_status, action_id, "rejected",
            extra_detail or f"rejected via {via}", org_id=org,
        )
        new_status, job_id = "rejected", None
    elif decision == "respond":
        detail = ("response: " + str(body.get("response_text") or "").strip())[:300]
        await run_in_threadpool(
            ledger.set_action_status, action_id, "done", detail, org_id=org
        )
        new_status, job_id = "done", None
    else:  # approve
        await run_in_threadpool(
            ledger.set_action_status, action_id, "approved",
            extra_detail
            or f"approved via {via}" + (f" by {laura_user}" if laura_user else ""),
            org_id=org,
        )
        if blocked:
            new_status, job_id = "approved", None  # [M8] executes when deps land
        else:
            job_id, new_status, capability_blocked = await run_in_threadpool(
                lambda: _execute_route(
                    org, action_id, action, acting_avatar,
                    idempotency_key=action_plane.execution_idempotency_key(
                        action_id, slot
                    ),
                    via=via or "org-door",
                )
            )

    await run_in_threadpool(
        lambda: ledger.set_action_decision_result(
            action_id, org_id=org, new_status=new_status,
            execution_job_id=job_id,
        )
    )
    resp: dict = {
        "ok": True, "action_id": action_id, "idempotent_replay": False,
        "approved": decision == "approve",
        "previous_status": prev, "new_status": new_status,
        "execution_job_id": job_id,
        # #255-relay compatibility extras (additive to the contract shape).
        "executed": new_status in ("done", "failed") and job_id is not None,
        "capability_blocked": capability_blocked,
    }
    if blocked:
        resp["blocked_on"] = blocked
    return JSONResponse(resp)


@router.post("/chat")
async def org_chat_post(request: Request) -> JSONResponse:
    """Cedric posts into the org's chat channel. NOTE: the dashboard's chat UI
    was removed 2026-07-20 (owner request); no in-repo surface renders these
    messages today; the channel is retained as the transport for the planned
    Cedric bridge (docs/CEDRIC-DASHBOARD-BRIDGE.md), and the referenced action
    itself still surfaces in the Action Center regardless.

    Body; exactly one of:
      {"message": {"text": "...", "sender_label"?: "Cedric"}}
      {"action_card": {"action_id": "...", "item": "...", "owner"?, "due"?,
                       "note"?: "<=300 chars lead-in shown above the card>"}}

    Decisions converge on the EXISTING canonical doors (action_approvals);
    this endpoint only carries the conversation. Distilled content only
    (never transcript text)."""
    err, org = await _machine_gate(request)
    if err:
        return err
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    message = body.get("message")
    card = body.get("action_card")
    if bool(message) == bool(card):
        return JSONResponse(
            {"error": "send exactly one of message | action_card"}, status_code=400
        )
    if message:
        if not isinstance(message, dict) or not str(message.get("text") or "").strip():
            return JSONResponse({"error": "message.text is required"}, status_code=400)
        row = await run_in_threadpool(
            lambda: store.add_chat_message(
                org, "cedric",
                body=str(message.get("text") or ""),
                sender_label=str(message.get("sender_label") or "Cedric")[:120],
            )
        )
    else:
        if not isinstance(card, dict):
            return JSONResponse({"error": "action_card must be an object"}, status_code=400)
        action_id = str(card.get("action_id") or "").strip()
        item = str(card.get("item") or "").strip()
        if not action_id or not item:
            return JSONResponse(
                {"error": "action_card.action_id and .item are required"},
                status_code=400,
            )
        row = await run_in_threadpool(
            lambda: store.add_chat_message(
                org, "cedric",
                body=str(card.get("note") or "")[:300],
                sender_label="Cedric",
                kind="action_card",
                action_id=action_id,
                payload={
                    "item": item[:300],
                    "owner": str(card.get("owner") or "")[:100],
                    "due": str(card.get("due") or "")[:100],
                },
            )
        )
    if row is None:
        return JSONResponse({"error": "message rejected"}, status_code=400)
    return JSONResponse({"ok": True, "id": row["id"]})
# ─────────── canonical Action reads + edits (control plane, M0) ───────────
# One canonical Action object per (org, action_id): the durable queued_actions
# row (status/receipt/logs/typed edits) merged with the saved artifact's
# richer fields (proposal, dependencies, approvers). Distilled data only -
# briefs and typed args, never transcript content.


def _canonical_action_view(org: str, action_id: str) -> dict | None:
    """Assemble the canonical Action for one org, or None when invisible."""
    from .. import action_reconcile

    # Throttled stale-executing settle first, so the view never shows a claim
    # held by a process that died mid-call (async-dispatch safety).
    action_reconcile.maybe_reconcile(org)
    found = _org_action(org, action_id)
    durable = ledger.get_durable_action(action_id, org_id=org)
    if found is None and durable is None:
        return None
    action, acting_avatar = found if found is not None else ({}, "")
    typed = ledger.effective_typed(action_id, action.get("typed"), org_id=org)
    schema = action_plane.params_schema(typed)
    missing = action_plane.missing_params(typed)
    srow = (ledger.action_statuses([action_id], org_id=org)
            .get(action_id) or {})
    status = str(
        (durable or {}).get("execution_status") or srow.get("status") or ""
    )
    if not status:
        status = "needs_details" if (typed and missing) else "proposed"
    receipt = (durable or {}).get("receipt_json")
    logs = (durable or {}).get("logs_json")
    return {
        "action_id": action_id,
        "org_id": org,
        "origin_avatar": str(
            (durable or {}).get("origin_avatar") or acting_avatar
        ),
        "source": {"bot_id": str((durable or {}).get("bot_id") or "")},
        "action": str(
            (durable or {}).get("action") or action.get("item") or ""
        ),
        "owner": str(action.get("owner") or (durable or {}).get("owner") or ""),
        "due": str(action.get("deadline") or (durable or {}).get("due") or ""),
        "tool": str((typed or {}).get("type") or ""),
        "route": str(
            (durable or {}).get("execution_route")
            or action.get("execution_route") or ""
        ),
        "params": dict((typed or {}).get("args") or {}),
        "params_schema": schema,
        "missing_params": missing,
        "risk": str((durable or {}).get("risk") or action_plane.risk_for(typed)),
        "permission": {
            "policy": str(action.get("execution_policy") or "approval_required"),
            "approver_user_ids": [
                str(x) for x in (action.get("approver_user_ids") or [])
            ],
        },
        "status": status,
        "detail": str(
            (durable or {}).get("execution_detail") or srow.get("detail") or ""
        ),
        "receipt": receipt if isinstance(receipt, dict) else {},
        "logs": logs if isinstance(logs, list) else [],
        "idempotency_key": str((durable or {}).get("idempotency_key") or ""),
        "decision": ledger.get_action_decision(action_id, org_id=org),
        "correlation_id": str(action.get("correlation_id") or action_id),
        "proposal": action.get("proposal"),
        "dependencies": [str(x) for x in (action.get("dependencies") or [])],
        "updated_at": srow.get("updated_at")
        or (durable or {}).get("execution_updated_at"),
    }


@router.get("/actions/{action_id}")
async def org_action_get(action_id: str, request: Request) -> JSONResponse:
    """The canonical Action object for one stable action_id (agreed contract:
    one action, every surface — Slack cards and the dashboard render THIS)."""
    err, org = await _machine_gate(request)
    if err:
        return err
    aid = (action_id or "").strip()
    if not aid:
        return JSONResponse({"error": "action_id is required"}, status_code=400)
    view = await run_in_threadpool(_canonical_action_view, org, aid)
    if view is None:
        return JSONResponse(
            {"error": "unknown action for this org"}, status_code=404
        )
    return JSONResponse({"action": view})


def apply_param_edits(org: str, action_id: str, args: dict) -> tuple[int, dict]:
    """Fill or edit an action's typed parameters (the needs_details loop).

    Shared by the machine door below and the dashboard door; the ONLY seam
    through which a typed spec may change, so the approve doors can keep
    trusting stored specs over client bodies. Sync (threadpool caller).
    Returns (http_status, payload)."""
    aid = (action_id or "").strip()
    if not isinstance(args, dict) or not args:
        return 400, {"error": "args object is required"}
    found = _org_action(org, aid)
    if found is None:
        return 404, {"error": "unknown action for this org"}
    action, _avatar = found
    typed = ledger.effective_typed(aid, action.get("typed"), org_id=org)
    if not isinstance(typed, dict) or not typed.get("type"):
        return 409, {"error": "untyped_action", "action_id": aid}
    schema = action_plane.params_schema(typed)
    if not schema:
        return 409, {"error": "unknown_action_type", "action_id": aid}
    cleaned, errors = action_plane.validate_param_edits(schema, args)
    if errors:
        return 422, {"error": "invalid_params", "details": errors[:10]}

    decision = ledger.get_action_decision(aid, org_id=org)
    if decision is not None:
        return 409, {"error": "already_decided", "decision": decision["decision"]}
    current = (ledger.action_statuses([aid], org_id=org)
               .get(aid) or {}).get("status") or ""
    if current in _TERMINAL or current == "executing":
        return 409, {"error": "not_editable", "status": current}

    merged = ledger.update_action_params(
        aid, cleaned, org_id=org, artifact_typed=action.get("typed")
    )
    if merged is None:
        return 409, {"error": "not_editable", "action_id": aid}
    missing = action_plane.missing_params(merged)
    new_status = "needs_details" if missing else "proposed"
    if current in ("", "needs_details"):
        ledger.set_action_status(
            aid, new_status,
            ("missing: " + ", ".join(missing)) if missing else "params complete",
            org_id=org,
        )
    else:
        new_status = current

    # action.updated (agreed events envelope): both surfaces re-render the
    # card from the same canonical fields. Best-effort single attempt, the
    # same discipline as the executor's action.status mirror.
    try:
        from ..cedric import callback as cedric_callback

        cedric_callback.send_action_event(org, "action.updated", {
            "action_id": aid,
            "status": new_status,
            "params": {k: merged.get("args", {}).get(k) for k in cleaned},
            "missing_params": missing,
        })
    except Exception:  # noqa: BLE001; the edit is committed; events are best-effort
        pass

    return 200, {
        "ok": True,
        "action_id": aid,
        "params": dict(merged.get("args") or {}),
        "missing_params": missing,
        "status": new_status,
    }


@router.post("/actions/{action_id}/params")
async def org_action_params(action_id: str, request: Request) -> JSONResponse:
    """Machine door for apply_param_edits — body {"args": {field: value}}."""
    err, org = await _machine_gate(request)
    if err:
        return err
    aid = (action_id or "").strip()
    if not aid:
        return JSONResponse({"error": "action_id is required"}, status_code=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001; malformed JSON is a client error
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    args = (body or {}).get("args") if isinstance(body, dict) else None
    code, payload = await run_in_threadpool(apply_param_edits, org, aid, args)
    return JSONResponse(payload, status_code=code)
