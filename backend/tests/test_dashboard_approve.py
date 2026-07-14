"""POST /dashboard/actions/{id}/approve — the native approve→execute loop.

Key-free: sqlite in tmp_path, Google mocked at executor.google_client, and the
NATIVE_EXECUTOR flag toggled per test. Asserts the two invariants that matter:
  1. auth + org scoping — a logged-in owner only, and only for their own org.
  2. the executor runs ONLY when the flag is on AND the action is typed; with
     the flag off (today's default) approve marks the row and executes nothing.
The receipt (event link / message id) lands in the same ledger provenance
channel the dashboard reads.
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
    monkeypatch.setattr(settings, "native_executor", False)
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


_EMAIL_TYPED = {
    "type": "email.send",
    "args": {"to": ["marco@acme.com"], "subject": "Recap", "body": "Notes"},
}


def _seed_action(org: str, action_id: str, typed: dict | None = None) -> None:
    action = {"item": "Email the recap to marco@acme.com", "owner": "Ben",
              "action_id": action_id}
    if typed is not None:
        action["typed"] = typed
    store.save_artifact(
        f"bot_{action_id}",
        {
            "summary": "Kickoff.",
            "actions": [action],
            "checklist": [action],
            "org_id": org,
            "avatar_id": "laura",
            "meeting_url": "https://meet.google.com/appr-test",
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


# ── auth ──

def test_approve_requires_login(client):
    _seed_action(settings.demo_org_id, "a1", _EMAIL_TYPED)
    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 401  # no cookie → login required


# ── flag OFF (today's default): approve marks the row, executes nothing ──

def test_flag_off_marks_approved_without_executing(client, monkeypatch):
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is True and body["executed"] is False
    assert body["execution_mode"] == "cedric"
    assert not calls  # google was never called with the flag off
    # The ledger recorded "approved" (non-terminal), not "done".
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st and st["status"] == "approved"


# ── flag ON + typed: the executor runs and writes the receipt ──

def test_flag_on_executes_typed_email_and_writes_receipt(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m-123"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is True and body["typed"] is True
    assert body["execution_mode"] == "native"
    assert len(calls) == 1 and calls[0][0] == user["org_id"]
    # Receipt: done + the message id, in the channel the dashboard reads.
    assert body["status"]["status"] == "done"
    assert "m-123" in body["status"]["detail"]
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "done" and "m-123" in st["detail"]


def test_flag_on_untyped_action_marks_approved_without_executing(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", typed=None)  # no typed spec
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is False and body["typed"] is False
    assert not calls


def test_flag_on_soft_failure_records_failed_receipt(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    _mock_send(monkeypatch, {"ok": False, "error": "gmail send failed (HTTP 500)"})

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is True  # we attempted it
    assert body["status"]["status"] == "failed" and "500" in body["status"]["detail"]


# ── org scoping ──

def test_cannot_approve_another_orgs_action(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    owner = store.upsert_user("owner@x.com")
    _seed_action(owner["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    # A DIFFERENT user (different personal org) tries to approve it.
    _login(client, "intruder@y.com")
    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 404
    assert not calls  # never executed for a foreign org


def test_unknown_action_is_404(client):
    _login(client)
    r = client.post("/dashboard/actions/does-not-exist/approve")
    assert r.status_code == 404
