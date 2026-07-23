"""[M8] deferred release — dependency-blocked approvals actually execute.

The approve door parks an approved action on `blocked_on` and executes nothing
(test_approve_door::test_approve_with_unmet_dependency_blocks_execution proves
the gate holds). Nothing ever re-dispatched it: the approval sat `approved`
forever — the gap named in docs/product/UNIFIED-ACTION-CONTROL-PLANE.md and the
obligation Laura carries in Cedric relay contract v3 (Cedric hard-stops on
`blocked_on`, so if Laura never releases, NOBODY executes).

These tests pin the release and, just as importantly, what must NOT happen:
never twice, never before the dependency is done, never on a failed dependency,
never past the owner's capability toggle. Key-free: temp SQLite, Google mocked.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import action_deps, executor, ledger, store
from app.config import settings

_TYPED = {"type": "email.send", "args": {"to": "dana@acme.com", "subject": "Recap",
                                         "body": "As discussed."}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    importlib.reload(action_deps)
    monkeypatch.setattr(settings, "native_executor", True)
    return TestClient(main_module.app)


def _seed(org: str, action_id: str, **extra) -> None:
    action = {"item": f"Do {action_id}", "owner": "Ben", "action_id": action_id,
              **extra}
    store.save_artifact(
        f"bot_{action_id}",
        {"summary": "Kickoff.", "actions": [action], "checklist": [action],
         "org_id": org, "avatar_id": "laura",
         "meeting_url": "https://meet.google.com/deps",
         "transcript": "PII must never leak"},
        org_id=org,
    )


def _approve(client: TestClient, action_id: str, **body):
    return client.post(f"/org/actions/{action_id}/approve",
                       json={"decision": "approve", **body})


@pytest.fixture
def sent(monkeypatch):
    """Every real email the native executor would send."""
    calls: list = []
    monkeypatch.setattr(
        executor.google_client, "send_gmail",
        lambda o, m: calls.append(m) or {"ok": True, "message_id": f"m{len(calls)}"},
    )
    return calls


# ── the release itself ──

def test_dependency_landing_executes_the_parked_approval(client, sent):
    """THE bug this module exists for: approved, parked, and then actually run."""
    org = settings.demo_org_id
    _seed(org, "dep1", typed=_TYPED, execution_route="native")
    _seed(org, "a1", typed=_TYPED, execution_route="native", dependencies=["dep1"])

    body = _approve(client, "a1").json()
    assert body["new_status"] == "approved" and body["blocked_on"] == ["dep1"]
    assert not sent, "parked approval must not execute"

    # The dependency completes — by ANY route. This is the release trigger.
    ledger.set_action_status("dep1", "done", "sent", org_id=org)

    assert len(sent) == 1, "the parked approval must run exactly once when its dep lands"
    assert (ledger.action_statuses(["a1"], org_id=org).get("a1") or {})["status"] == "done"
    # Unparked: the decision row must not keep claiming it waits on anything.
    assert store.get_action_approval(org, "a1")["blocked_on"] in ("", "[]")


def test_release_fires_for_a_dependency_cedric_reported(client, sent):
    """The case that chose the hook. Cedric executes cedric-routed work itself
    and reports it on /status — that path never touches
    set_action_decision_result (the hook the spec suggested), so hanging the
    release there would strand every dependent of Cedric-completed work."""
    org = settings.demo_org_id
    _seed(org, "cedric_dep")
    _seed(org, "a1", typed=_TYPED, execution_route="native",
          dependencies=["cedric_dep"])
    _approve(client, "a1")
    assert not sent

    r = client.post("/org/actions/cedric_dep/status",
                    json={"status": "done", "detail": "ran on Cedric"})
    assert r.status_code == 200
    assert len(sent) == 1, "a dependency Cedric completed must release its dependents"


def test_chain_releases_in_one_sweep(client, sent):
    """A→B→C: releasing B completes B, which must release C — iteratively, not
    by recursing through set_action_status."""
    org = settings.demo_org_id
    _seed(org, "a", typed=_TYPED, execution_route="native")
    _seed(org, "b", typed=_TYPED, execution_route="native", dependencies=["a"])
    _seed(org, "c", typed=_TYPED, execution_route="native", dependencies=["b"])
    _approve(client, "b")
    _approve(client, "c")
    assert not sent

    ledger.set_action_status("a", "done", "", org_id=org)

    assert len(sent) == 2, "both B and C must run once A lands"
    for aid in ("b", "c"):
        assert (ledger.action_statuses([aid], org_id=org).get(aid) or {})["status"] == "done"


def test_multi_dependency_waits_for_the_last_one(client, sent):
    org = settings.demo_org_id
    _seed(org, "d1")
    _seed(org, "d2")
    _seed(org, "a1", typed=_TYPED, execution_route="native",
          dependencies=["d1", "d2"])
    assert _approve(client, "a1").json()["blocked_on"] == ["d1", "d2"]

    ledger.set_action_status("d1", "done", "", org_id=org)
    assert not sent, "one of two dependencies is not enough"
    # Re-parked on the shrunken list — the dashboard should show what's left.
    assert store.get_action_approval(org, "a1")["blocked_on"] == '["d2"]'

    ledger.set_action_status("d2", "done", "", org_id=org)
    assert len(sent) == 1


# ── what must NOT happen ──

def test_failed_dependency_never_releases(client, sent):
    """A dependency that FAILED has not been done. Its dependents stay parked —
    running them anyway would execute work whose premise never happened."""
    org = settings.demo_org_id
    _seed(org, "dep1")
    _seed(org, "a1", typed=_TYPED, execution_route="native", dependencies=["dep1"])
    _approve(client, "a1")

    ledger.set_action_status("dep1", "failed", "smtp died", org_id=org)
    assert not sent
    ledger.set_action_status("dep1", "rejected", "no thanks", org_id=org)
    assert not sent
    assert store.get_action_approval(org, "a1")["blocked_on"] == '["dep1"]'


def test_release_is_not_a_second_execution(client, sent):
    """The release runs behind the same execution claim as the door, so an
    action already executed can never be run a second time by a later sweep."""
    org = settings.demo_org_id
    _seed(org, "dep1")
    _seed(org, "a1", typed=_TYPED, execution_route="native", dependencies=["dep1"])
    _approve(client, "a1")
    ledger.set_action_status("dep1", "done", "", org_id=org)
    assert len(sent) == 1

    # Any later completion in the org re-triggers a sweep; a1 must not re-run.
    _seed(org, "dep2")
    ledger.set_action_status("dep2", "done", "", org_id=org)
    ledger.set_action_status("dep1", "done", "again", org_id=org)
    assert len(sent) == 1, "a released action must never execute twice"


def test_capability_toggle_still_wins_at_release_time(client, sent, monkeypatch):
    """The owner's toggle is checked when the action RUNS, not when approved."""
    org = settings.demo_org_id
    _seed(org, "dep1")
    _seed(org, "a1", typed=_TYPED, execution_route="native", dependencies=["dep1"])
    _approve(client, "a1")
    monkeypatch.setattr(
        store, "get_avatar_capabilities", lambda a, org="": {"gmail": False}
    )

    ledger.set_action_status("dep1", "done", "", org_id=org)

    assert not sent, "a capability-blocked action must not execute on release"
    status = (ledger.action_statuses(["a1"], org_id=org).get("a1") or {})
    assert status["status"] == "approved"
    assert "tool disabled" in status["detail"]


def test_cedric_routed_release_says_it_awaits_its_own_executor(client, sent):
    """Laura cannot execute a cedric-routed action. Its deps landing unparks it,
    but the receipt must not imply Laura ran it — and it must not be re-swept
    forever."""
    org = settings.demo_org_id
    _seed(org, "dep1")
    _seed(org, "a1", typed=_TYPED, execution_route="cedric", dependencies=["dep1"])
    _approve(client, "a1")

    ledger.set_action_status("dep1", "done", "", org_id=org)

    assert not sent
    status = (ledger.action_statuses(["a1"], org_id=org).get("a1") or {})
    assert status["status"] == "approved"
    assert "awaiting its own executor" in status["detail"]
    assert store.get_action_approval(org, "a1")["blocked_on"] in ("", "[]")


def test_dependency_cycle_cannot_spin(client, sent):
    """A↔B: neither can ever be done, so neither is ever released. The guard is
    that nothing spins or recurses while proving it."""
    org = settings.demo_org_id
    _seed(org, "a", typed=_TYPED, execution_route="native", dependencies=["b"])
    _seed(org, "b", typed=_TYPED, execution_route="native", dependencies=["a"])
    assert _approve(client, "a").json()["blocked_on"] == ["b"]
    assert _approve(client, "b").json()["blocked_on"] == ["a"]

    _seed(org, "unrelated")
    ledger.set_action_status("unrelated", "done", "", org_id=org)
    assert not sent


def test_rejected_action_is_never_released(client, sent):
    """Only approve-decisions are parked. A rejected action carries no blocked_on
    and must never be swept into execution."""
    org = settings.demo_org_id
    _seed(org, "dep1")
    _seed(org, "a1", typed=_TYPED, execution_route="native", dependencies=["dep1"])
    client.post("/org/actions/a1/approve", json={"decision": "reject"})

    ledger.set_action_status("dep1", "done", "", org_id=org)
    assert not sent


def test_unapproved_action_is_never_released(client, sent):
    """No decision at all → nothing parked → a landing dependency runs nothing.
    The human's approval remains the only thing that starts an action."""
    org = settings.demo_org_id
    _seed(org, "dep1")
    _seed(org, "a1", typed=_TYPED, execution_route="native", dependencies=["dep1"])

    ledger.set_action_status("dep1", "done", "", org_id=org)
    assert not sent


def test_release_never_breaks_the_status_write(client, monkeypatch):
    """A dependent's execution blowing up must not fail the status write that
    triggered it — the dependency really did complete."""
    org = settings.demo_org_id
    _seed(org, "dep1")
    _seed(org, "a1", typed=_TYPED, execution_route="native", dependencies=["dep1"])
    _approve(client, "a1")

    def boom(*a, **k):
        raise RuntimeError("executor exploded")

    monkeypatch.setattr(executor, "execute_approved", boom)
    assert ledger.set_action_status("dep1", "done", "", org_id=org) is True
    assert (ledger.action_statuses(["dep1"], org_id=org).get("dep1") or {})["status"] == "done"


def test_no_parked_rows_is_a_cheap_no_op(client, sent):
    org = settings.demo_org_id
    _seed(org, "solo", typed=_TYPED, execution_route="native")
    assert action_deps.release_dependents(org, "solo") == []
    ledger.set_action_status("solo", "done", "", org_id=org)
    assert not sent
