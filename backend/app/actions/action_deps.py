"""Deferred execution of dependency-blocked approvals — the [M8] release.

The approve door resolves an approved action's `dependencies` and, when any of
them isn't `done` yet, records the decision with a `blocked_on` list and
deliberately does NOT execute. Nothing ever re-dispatched those approvals: they
sat `approved` forever, which is the gap named in
`docs/product/UNIFIED-ACTION-CONTROL-PLANE.md` ("the [M8] gate records
`blocked_on` but nothing re-dispatches when dependencies land") and the
obligation Laura carries in the Cedric relay contract v3
(`hsk_con_ycbd1shbdbh4p5p5cgg2`): Cedric hard-stops on `blocked_on` because
ordering is Laura's to enforce, so if Laura never releases, the action is
approved and executes NOWHERE.

TRIGGER — `ledger.set_action_status` reaching a terminal `done`, the ONE weld
point every completion already goes through: Laura's native executor receipt,
the `respond` shortcut, the stale-`executing` reconciler's settle, and — the
case that decides it — Cedric POSTing `/org/actions/{id}/status` for a
cedric-routed action it executed itself. The spec suggested
`set_action_decision_result` instead; that hook only fires for actions LAURA
executed, so a dependency completed by Cedric would never have released its
dependents. Releases run only for terminal-`done`: a `failed`/`rejected`
dependency must leave its dependents parked, not run them.

SAFETY, in the order the failure modes actually bite:
- **Never two writes.** Every release executes behind
  `ledger.claim_action_execution` — the same compare-and-set the door uses — so
  a release racing a dashboard approve of the same action produces exactly one
  external write; the loser simply reports the winner's status.
- **Never unbounded recursion.** Releasing B completes B, which re-enters this
  module through `set_action_status`. A re-entrancy flag makes the nested call
  a no-op and the OUTER sweep re-scans instead, so a chain A→B→C settles in one
  bounded loop. A dependency cycle can never spin: a cycle means no member ever
  reaches `done`, so nothing is ever released.
- **Never break the caller.** A status write must not fail because a
  dependent's calendar call did — every release is best-effort and swallows.
- **Never the live path.** Reached only from finalize/dashboard/relay writes,
  never from `ws/<conversation_id>`.
"""
from __future__ import annotations

import json
import threading
from typing import Any

# Re-entrancy guard: a release completes an action, which re-enters
# set_action_status → release_dependents. Thread-local (not a global lock) so
# two orgs releasing concurrently never serialize on each other, while a single
# chain collapses into its outermost sweep.
_local = threading.local()

# A sweep re-scans after every release, so this only bounds pathological data
# (a very long dependency chain); each pass releases at least one action.
_MAX_PASSES = 25


def _blocked_on(raw: Any) -> list[str]:
    """The `blocked_on` list off a decision row — stored as a JSON array of
    action_ids, tolerated as a bare list. Junk reads as 'not blocked': a row we
    cannot parse must not strand an approval forever."""
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 — malformed row, not a client error
        return []
    return [str(x) for x in parsed if str(x).strip()] if isinstance(parsed, list) else []


def _release_one(org_id: str, action_id: str) -> bool:
    """Settle one parked approval whose trigger just landed. True only when it
    actually reached `done` (the sweep re-scans for what THAT unblocks next).

    Clearing `blocked_on` is unconditional once the dependencies are met — even
    when Laura ends up executing nothing. An approval that stays parked would be
    re-swept on every future completion in the org, forever; and the durable
    `set_action_decision_result` does not clear `blocked_on` on its own, so the
    clear has to be explicit here rather than a side effect of running.
    """
    from . import ledger
    from .. import org_api

    found = org_api._org_action(org_id, action_id)
    if found is None:
        # The artifact is gone or no longer visible to this org. Unpark and
        # settle honestly rather than leave a row nothing can ever sweep again.
        _clear_parked(org_id, action_id)
        ledger.set_action_decision_result(
            action_id, org_id=org_id, new_status="failed", execution_job_id=None
        )
        ledger.set_action_status(
            action_id, "failed",
            "dependencies met, but the action is no longer on a visible artifact",
            org_id=org_id,
        )
        return False
    action, acting_avatar = found

    still = org_api._unmet_dependencies(org_id, action)
    if still:
        # Another dependency is still outstanding — re-park on the SHRUNKEN list
        # so the dashboard shows what is genuinely still awaited.
        _repark(org_id, action_id, still)
        return False

    _clear_parked(org_id, action_id)

    decision = ledger.get_action_decision(action_id, org_id=org_id) or {}
    slot = str(decision.get("selected_slot_id") or "")
    # Replay the door's [M9] slot materialization: an action released later must
    # still create the event the human actually picked.
    action = _with_slot(action, slot)

    from . import action_plane

    job_id, new_status, capability_blocked = org_api._execute_route(
        org_id, action_id, action, acting_avatar,
        idempotency_key=action_plane.execution_idempotency_key(action_id, slot),
        via="dependency-release",
    )
    if capability_blocked:
        # Same rule as the door and the Cedric relay (contract v3): the owner's
        # toggle wins. Approved, nothing ran, nothing retried.
        ledger.set_action_status(
            action_id, "approved",
            "dependencies met — not executed: tool disabled for this avatar",
            org_id=org_id,
        )
        return False
    if new_status == "approved" and job_id is None:
        # Laura executed nothing and this is NOT a capability veto: the action is
        # cedric-routed, or native with no typed spec the executor handles. Its
        # dependencies are met and it is no longer parked, but Laura has no
        # channel to dispatch it — Cedric's own loop owns a cedric-routed action
        # (tenancy-v4 dispatch-action is the sanctioned push, not yet accepted).
        # Say so on the provenance channel instead of implying it ran.
        ledger.set_action_status(
            action_id, "approved",
            "dependencies met — awaiting its own executor", org_id=org_id,
        )
        return False
    ledger.set_action_decision_result(
        action_id, org_id=org_id, new_status=new_status, execution_job_id=job_id
    )
    # 'executing' (async dispatch) is not a completion: that worker's own
    # done-receipt re-enters set_action_status and drives the next release.
    return new_status == "done"


def _with_slot(action: dict, slot_id: str) -> dict:
    """The door's [M9] slot materialization, replayed at release time."""
    proposal = action.get("proposal") if isinstance(action.get("proposal"), dict) else None
    if not (proposal and slot_id):
        return action
    for s in proposal.get("candidate_slots") or []:
        if isinstance(s, dict) and str(s.get("slot_id")) == slot_id:
            typed = action.get("typed") if isinstance(action.get("typed"), dict) else {}
            args = {**(typed.get("args") or {}), "start": s.get("start"), "end": s.get("end")}
            return {**action, "typed": {**typed, "args": args}}
    return action


def _repark(org_id: str, action_id: str, still: list[str]) -> None:
    """Shrink a parked approval's blocked_on to what is genuinely outstanding."""
    from . import ledger
    from .. import store

    if ledger._durable_actions(org_id):
        from . import outbox_pg

        outbox_pg.set_action_decision_blocked_on(org_id, action_id, json.dumps(still))
        return
    store.set_action_approval_blocked_on(org_id, action_id, json.dumps(still))


def _clear_parked(org_id: str, action_id: str) -> None:
    """Drop the parked state without claiming an execution job."""
    _repark(org_id, action_id, [])


def release_dependents(org_id: str, done_action_id: str) -> list[str]:
    """Release approvals parked on ``done_action_id`` now that it is `done`.

    Returns the action_ids actually executed (for tests/observability). Safe to
    call on every terminal `done`: with no parked rows it is one org-scoped
    query and out. Never raises.
    """
    done_id = (done_action_id or "").strip()
    org = (org_id or "").strip()
    if not done_id or not org:
        return []
    # Nested call from an action this sweep just completed: the outer loop
    # re-scans, so collapsing here is what keeps a chain iterative.
    if getattr(_local, "sweeping", False):
        return []
    _local.sweeping = True
    try:
        return _sweep(org, done_id)
    except Exception:  # noqa: BLE001 — a status write must never fail on this
        return []
    finally:
        _local.sweeping = False


def _sweep(org: str, done_id: str) -> list[str]:
    from . import ledger

    executed: list[str] = []
    # `landed` is what this sweep has newly completed: pass 1 releases what
    # waited on the trigger, pass 2 what waited on THOSE, and so on.
    landed = {done_id}
    for _ in range(_MAX_PASSES):
        try:
            parked = ledger.list_blocked_decisions(org)
        except Exception:  # noqa: BLE001 — storage hiccup: leave rows parked
            break
        ready = [
            str(row.get("action_id") or "")
            for row in parked
            if landed & set(_blocked_on(row.get("blocked_on")))
        ]
        ready = [a for a in ready if a and a not in executed]
        if not ready:
            break
        progressed = False
        for action_id in ready:
            try:
                ran = _release_one(org, action_id)
            except Exception:  # noqa: BLE001 — one bad action must not stop the rest
                continue
            if ran:
                executed.append(action_id)
                landed.add(action_id)
                progressed = True
        if not progressed:
            break
    return executed
