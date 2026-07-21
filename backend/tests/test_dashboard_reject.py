"""POST /dashboard/actions/{id}/reject; the approve door's mirror.

Key-free: sqlite in tmp_path, Google mocked at executor.google_client. Asserts:
  1. auth + org scoping: a logged-in owner only, and only for their own org.
  2. reject marks the action `rejected` (terminal) and closes the ledger row;
     nothing ever executes on this path.
  3. approve-after-reject is refused (409) and never reaches the executor -
     without that guard the monotonic chip would stay 'rejected' but the
     action would still RUN.
  4. reject-after-done is a monotonic no-op reported honestly.
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
    importlib.reload(ledger)  # shares the sqlite file; needs its tables too
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
            "meeting_url": "https://meet.google.com/rej-test",
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

def test_reject_requires_login(client):
    _seed_action(settings.demo_org_id, "a1", _EMAIL_TYPED)
    r = client.post("/dashboard/actions/a1/reject")
    assert r.status_code == 401  # no cookie → login required


# ── the happy path: rejected, terminal, nothing executed ──

def test_reject_marks_rejected_and_never_executes(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)  # even with it ON
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    r = client.post("/dashboard/actions/a1/reject")
    assert r.status_code == 200
    body = r.json()
    assert body["rejected"] is True
    assert body["status"]["status"] == "rejected"
    assert not calls  # reject never touches the executor
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st and st["status"] == "rejected"


def test_reject_works_for_untyped_actions_too(client):
    user = _login(client)
    _seed_action(user["org_id"], "a1", typed=None)
    r = client.post("/dashboard/actions/a1/reject")
    assert r.status_code == 200
    assert r.json()["rejected"] is True


# ── approve after reject: refused, executor never runs ──

def test_approve_after_reject_is_409_and_never_executes(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    assert client.post("/dashboard/actions/a1/reject").status_code == 200
    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 409
    assert not calls  # the rejected action was never run
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st["status"] == "rejected"  # chip untouched


# ── reject after done: monotonic no-op, reported honestly ──

def test_reject_after_done_reports_done_not_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", _EMAIL_TYPED)
    _mock_send(monkeypatch, {"ok": True, "message_id": "m-123"})

    assert client.post("/dashboard/actions/a1/approve").status_code == 200
    r = client.post("/dashboard/actions/a1/reject")
    assert r.status_code == 200
    body = r.json()
    assert body["rejected"] is False  # honest: the mark did not take
    assert body["status"]["status"] == "done"


# ── org scoping ──

def test_cannot_reject_another_orgs_action(client):
    owner = store.upsert_user("owner@x.com")
    _seed_action(owner["org_id"], "a1", _EMAIL_TYPED)

    _login(client, "intruder@y.com")
    r = client.post("/dashboard/actions/a1/reject")
    assert r.status_code == 404
    st = ledger.action_statuses(["a1"], org_id=owner["org_id"]).get("a1")
    assert not st or st.get("status") != "rejected"  # owner's row untouched


def test_unknown_action_is_404(client):
    _login(client)
    r = client.post("/dashboard/actions/does-not-exist/reject")
    assert r.status_code == 404
