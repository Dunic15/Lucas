"""Security audit writer: metadata-only, non-blocking, first-write semantics."""
from __future__ import annotations

import importlib
import queue
import sys
from pathlib import Path
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.persistence import audit_log


def test_event_accepts_only_safe_metadata():
    org = str(uuid.uuid4())
    actor = str(uuid.uuid4())
    event = audit_log._event(org, actor, "action.approve", "a_123-xyz")
    assert event is not None
    assert event.org_id == org
    assert event.actor_user_id == actor
    assert event.action == "action.approve"
    assert event.target == "a_123-xyz"

    # Human-readable content and addresses must never become audit targets.
    assert audit_log._event(org, actor, "action.read", "Quarterly plan") is None
    assert audit_log._event(org, actor, "action.read", "person@example.com") is None
    assert audit_log._event(org, actor, "Transcript text", "a1") is None


def test_invalid_actor_becomes_machine_actor():
    event = audit_log._event(
        str(uuid.uuid4()), "slack-user-not-a-uuid", "action.approve", "a1"
    )
    assert event is not None
    assert event.actor_user_id is None


def test_disabled_writer_is_a_noop(monkeypatch):
    monkeypatch.setattr(audit_log.control_plane, "enabled", lambda: False)
    monkeypatch.setattr(
        audit_log, "_ensure_worker",
        lambda: (_ for _ in ()).throw(AssertionError("must not start")),
    )
    assert not audit_log.record(
        str(uuid.uuid4()), actor_user_id=None, action="action.read", target="a1"
    )


def test_queue_is_bounded_and_never_blocks(monkeypatch):
    pending: queue.Queue = queue.Queue(maxsize=1)
    monkeypatch.setattr(audit_log, "_QUEUE", pending)
    monkeypatch.setattr(audit_log.control_plane, "enabled", lambda: True)
    monkeypatch.setattr(audit_log, "_ensure_worker", lambda: None)
    org = str(uuid.uuid4())

    assert audit_log.record(
        org, actor_user_id=None, action="action.read", target="a1"
    )
    assert not audit_log.record(
        org, actor_user_id=None, action="action.read", target="a2"
    )
    assert pending.qsize() == 1


def test_write_is_tenant_scoped(monkeypatch):
    calls: list[tuple] = []

    class Connection:
        def execute(self, statement, params):
            calls.append(("execute", str(statement), params))

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, exc_type, exc, tb):
            return False

    class Engine:
        def begin(self):
            return Transaction()

    monkeypatch.setattr(audit_log.control_plane, "_get_engine", lambda: Engine())
    monkeypatch.setattr(
        audit_log.control_plane,
        "_set_org",
        lambda conn, org: calls.append(("set_org", org)),
    )
    event = audit_log.AuditEvent(
        str(uuid.uuid4()), str(uuid.uuid4()), "action.reject", "a9"
    )
    audit_log._write(event)

    assert calls[0] == ("set_org", event.org_id)
    params = calls[1][2]
    assert params == {
        "org": event.org_id,
        "actor": event.actor_user_id,
        "action": "action.reject",
        "target": "a9",
    }


def test_canonical_decision_audits_only_the_winner(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "audit.sqlite3"))
    from app.persistence import store as store_module
    from app.actions import ledger as ledger_module

    importlib.reload(store_module)
    importlib.reload(ledger_module)
    captured: list[dict] = []
    monkeypatch.setattr(
        audit_log,
        "record",
        lambda org_id, **fields: captured.append(
            {"org_id": org_id, **fields}
        ) or True,
    )
    org = "u_local-test"
    actor = str(uuid.uuid4())

    assert ledger_module.record_action_decision(
        "action-1",
        org_id=org,
        decision="approve",
        decided_via="dashboard",
        laura_user_id=actor,
    )
    assert not ledger_module.record_action_decision(
        "action-1",
        org_id=org,
        decision="reject",
        decided_via="dashboard",
        laura_user_id=actor,
    )
    assert captured == [{
        "org_id": org,
        "actor_user_id": actor,
        "action": "action.approve",
        "target": "action-1",
    }]
