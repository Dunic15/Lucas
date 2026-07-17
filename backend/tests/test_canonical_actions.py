"""Canonical Action Control Plane (M0) — needs_details, params door, claim.

The invariants from docs/product/UNIFIED-ACTION-CONTROL-PLANE.md on the
key-free SQLite path:

  1. Approving a typed action with missing REQUIRED parameters is refused
     (422 needs_details + the exact fields) instead of executing a broken
     vendor call or silently no-opping — the 2026-07-16 "approved but nothing
     executed" failure class.
  2. The params door is the ONLY way a typed spec changes: schema-validated,
     refused after a decision, and the approve door then executes the EDITED
     params (never a client body on the approve door itself).
  3. GET /org/actions/{id} exposes ONE canonical Action object.
  4. Peer statuses are normalized at the boundary ('executed' → 'done').
  5. Double approval — same door or across doors — produces exactly one
     external write (decision record + execution claim).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, executor, ledger, store
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file — needs its tables too
    monkeypatch.setattr(settings, "native_executor", True)
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


_COMPLETE = {
    "type": "email.send",
    "args": {"to": ["marco@acme.com"], "subject": "Recap", "body": "Notes"},
}
_MISSING_TO = {
    "type": "email.send",
    "args": {"to": [], "subject": "Recap", "body": "Notes"},
}


def _seed_action(org: str, action_id: str, typed: dict | None = None) -> None:
    action = {"item": "Email the recap", "owner": "Ben", "action_id": action_id}
    if typed is not None:
        action["typed"] = typed
    store.save_artifact(
        f"bot_{action_id}",
        {
            "summary": "Kickoff.",
            "actions": [action],
            "org_id": org,
            "avatar_id": "laura",
            "meeting_url": "https://meet.google.com/canon-test",
            "transcript": "PII must never leak",
        },
        org_id=org,
    )


def _mock_send(monkeypatch, result: dict, sink: list | None = None):
    def fake_send(org, message):
        if sink is not None:
            sink.append((org, message))
        return result

    monkeypatch.setattr(executor.google_client, "send_gmail", fake_send)


# ── 1. needs_details gate ──

def test_dashboard_approve_refuses_missing_required_params(client, monkeypatch):
    user = _login(client)
    _seed_action(user["org_id"], "nd1", _MISSING_TO)
    sink: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, sink)

    r = client.post("/dashboard/actions/nd1/approve")
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "needs_details"
    assert body["missing_params"] == ["to"]
    assert any(f["name"] == "to" for f in body["params_schema"])
    assert sink == []  # nothing executed
    status = ledger.action_statuses(["nd1"], org_id=user["org_id"])["nd1"]
    assert status["status"] == "needs_details"


def test_org_door_approve_refuses_missing_required_params(client, monkeypatch):
    _seed_action(settings.demo_org_id, "nd2", _MISSING_TO)
    sink: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, sink)

    r = client.post("/org/actions/nd2/approve", json={"decision": "approve"})
    assert r.status_code == 422
    assert r.json()["error"] == "needs_details"
    assert r.json()["missing_params"] == ["to"]
    assert sink == []


# ── 2. the params door ──

def test_params_fill_then_approve_executes_edited_spec(client, monkeypatch):
    _seed_action(settings.demo_org_id, "p1", _MISSING_TO)
    sink: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, sink)

    r = client.post(
        "/org/actions/p1/params", json={"args": {"to": ["ceo@acme.com"]}}
    )
    assert r.status_code == 200
    assert r.json()["missing_params"] == []
    assert r.json()["status"] == "proposed"

    r = client.post("/org/actions/p1/approve", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["new_status"] == "done"
    # The EDITED params executed — not the artifact's original empty list.
    assert sink and sink[0][1]["to"] == ["ceo@acme.com"]


def test_params_door_validates_fields(client):
    _seed_action(settings.demo_org_id, "p2", _MISSING_TO)

    r = client.post("/org/actions/p2/params", json={"args": {"cc": ["x@y.z"]}})
    assert r.status_code == 422 and r.json()["error"] == "invalid_params"

    r = client.post("/org/actions/p2/params", json={"args": {"to": "not-a-list"}})
    assert r.status_code == 422 and r.json()["error"] == "invalid_params"

    _seed_action(settings.demo_org_id, "p3", typed=None)  # untyped/free-text
    r = client.post("/org/actions/p3/params", json={"args": {"to": ["a@b.c"]}})
    assert r.status_code == 409 and r.json()["error"] == "untyped_action"


def test_params_door_locked_after_decision(client, monkeypatch):
    _seed_action(settings.demo_org_id, "p4", _COMPLETE)
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"})
    assert client.post(
        "/org/actions/p4/approve", json={"decision": "approve"}
    ).status_code == 200

    r = client.post(
        "/org/actions/p4/params", json={"args": {"subject": "changed"}}
    )
    assert r.status_code == 409 and r.json()["error"] == "already_decided"


# ── 3. canonical GET ──

def test_canonical_action_get(client):
    _seed_action(settings.demo_org_id, "g1", _MISSING_TO)

    r = client.get("/org/actions/g1")
    assert r.status_code == 200
    view = r.json()["action"]
    assert view["action_id"] == "g1"
    assert view["tool"] == "email.send"
    assert view["missing_params"] == ["to"]
    assert view["status"] == "needs_details"
    assert view["risk"] == "medium"
    assert view["correlation_id"] == "g1"
    assert any(f["name"] == "subject" for f in view["params_schema"])
    # The distilled action text rides along; the transcript never does.
    assert "PII" not in str(view)

    assert client.get("/org/actions/unknown-id").status_code == 404


def test_canonical_get_is_org_scoped(client):
    _seed_action("11111111-1111-1111-1111-111111111111", "g2", _COMPLETE)
    # The key-free caller resolves to the Demo org — another org's action is
    # indistinguishable from an unknown id.
    assert client.get("/org/actions/g2").status_code == 404


# ── 4. boundary normalization ──

def test_status_report_normalizes_executed_to_done(client):
    _seed_action(settings.demo_org_id, "s1", _COMPLETE)
    r = client.post("/org/actions/s1/status", json={"status": "executed"})
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    status = ledger.action_statuses(["s1"], org_id=settings.demo_org_id)["s1"]
    assert status["status"] == "done"


def test_status_report_still_rejects_unknown_values(client):
    _seed_action(settings.demo_org_id, "s2", _COMPLETE)
    r = client.post("/org/actions/s2/status", json={"status": "sideways"})
    assert r.status_code == 400


# ── 5. double approval → exactly one external write ──

def test_dashboard_double_approve_executes_once(client, monkeypatch):
    user = _login(client)
    _seed_action(user["org_id"], "d1", _COMPLETE)
    sink: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, sink)

    first = client.post("/dashboard/actions/d1/approve")
    assert first.status_code == 200 and first.json()["executed"] is True
    second = client.post("/dashboard/actions/d1/approve")
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["executed"] is False
    assert len(sink) == 1


def test_org_door_double_approve_executes_once(client, monkeypatch):
    _seed_action(settings.demo_org_id, "d2", _COMPLETE)
    sink: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, sink)

    first = client.post("/org/actions/d2/approve", json={"decision": "approve"})
    assert first.status_code == 200 and first.json()["new_status"] == "done"
    second = client.post("/org/actions/d2/approve", json={"decision": "approve"})
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert len(sink) == 1


def test_claim_is_single_winner_and_respects_terminal(client):
    org = settings.demo_org_id
    assert ledger.claim_action_execution("c1", org_id=org, via="test") is True
    assert ledger.claim_action_execution("c1", org_id=org, via="test") is False

    ledger.set_action_status("c2", "done", "already ran", org_id=org)
    assert ledger.claim_action_execution("c2", org_id=org, via="test") is False
