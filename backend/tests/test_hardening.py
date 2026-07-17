"""Key-free half of the M0/M1 hardening: SQLite stale-executing settle.

Without the control plane, execution claims live in the per-instance SQLite
action_status channel. There is nothing to verify against (no durable typed
row), so the rule is grace-only: an 'executing' row older than the grace
window settles failed with the explicit execution_unknown detail; a fresh
claim is left alone. The reconciler must never raise from a read path.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import action_reconcile, ledger, store
from app.config import settings


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    ledger._init_db()
    action_reconcile._reset_for_tests()


def _seed_executing(org: str, action_id: str, age_seconds: float) -> None:
    with store._LOCK, store._connect() as conn:
        conn.execute(
            "INSERT INTO action_status "
            "(org_id, action_id, status, detail, updated_at) "
            "VALUES (?,?,?,?,?)",
            (org, action_id, "executing", "executing via test",
             time.time() - age_seconds),
        )


def test_stale_executing_settles_failed_unknown():
    org = settings.demo_org_id
    _seed_executing(org, "old-1", action_reconcile.UNKNOWN_GRACE_SECONDS + 60)
    assert action_reconcile.maybe_reconcile(org) == 1
    status = ledger.action_statuses(["old-1"], org_id=org)["old-1"]
    assert status["status"] == "failed"
    assert "execution_unknown" in status["detail"]


def test_fresh_executing_claim_is_left_alone():
    org = settings.demo_org_id
    _seed_executing(org, "new-1", 5)
    assert action_reconcile.maybe_reconcile(org) == 0
    status = ledger.action_statuses(["new-1"], org_id=org)["new-1"]
    assert status["status"] == "executing"


def test_throttle_and_never_raises(monkeypatch):
    org = settings.demo_org_id
    _seed_executing(org, "old-2", action_reconcile.UNKNOWN_GRACE_SECONDS + 60)
    assert action_reconcile.maybe_reconcile(org) == 1
    # Inside the TTL the pass is skipped entirely.
    assert action_reconcile.maybe_reconcile(org) == 0
    # And a broken store surfaces as 0, never an exception on a read path.
    action_reconcile._reset_for_tests()
    monkeypatch.setattr(
        action_reconcile, "_reconcile_sqlite",
        lambda org_id: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert action_reconcile.maybe_reconcile(org) == 0
