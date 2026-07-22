"""Per-avatar capability toggles (dashboard): connect once at the org level,
then each avatar independently turns a capability ON/OFF.

Key-free (sqlite in tmp_path, Google mocked at executor.google_client). Covers:
  1. store set/get + the default-ON-when-connected resolver.
  2. POST /dashboard/avatar/{id}/capability — owner-only + validation.
  3. dashboard summary exposes capabilities_toggle.
  4. ENFORCEMENT: a Google action is SKIPPED when the avatar's google flag is
     off (and runs when it isn't); Slack delivery is skipped when slack is off.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, autopilot, executor, ledger, store
from app.config import settings


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file — needs its tables too
    monkeypatch.setattr(settings, "native_executor", False)


@pytest.fixture
def client():
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


_EMAIL_TYPED = {
    "type": "email.send",
    "args": {"to": ["marco@acme.com"], "subject": "Recap", "body": "Notes"},
}


def _seed_action(org: str, action_id: str, avatar_id: str = "laura") -> None:
    action = {"item": "Email the recap", "owner": "Ben",
              "action_id": action_id, "typed": _EMAIL_TYPED}
    store.save_artifact(
        f"bot_{action_id}",
        {
            "summary": "Kickoff.",
            "actions": [action],
            "checklist": [action],
            "org_id": org,
            "avatar_id": avatar_id,
            "meeting_url": "https://meet.google.com/cap-test",
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


# ── store: set/get + default-on-when-connected ─────────────────────────

def test_set_get_capability_roundtrip():
    assert store.get_avatar_capabilities("laura") == {}
    assert store.set_avatar_capability("laura", "google", True)
    assert store.get_avatar_capabilities("laura") == {"google": True}
    assert store.set_avatar_capability("laura", "google", False)  # upsert
    assert store.set_avatar_capability("laura", "slack", True)
    assert store.get_avatar_capabilities("laura") == {"google": False, "slack": True}
    assert store.all_avatar_capabilities() == {"laura": {"google": False, "slack": True}}


def test_set_capability_rejects_junk():
    # Slug-shaped keys are ACCEPTED (generic Pipedream apps are per-avatar
    # toggles under their name_slug); junk shapes are still rejected.
    assert store.set_avatar_capability("laura", "notion", True)
    assert not store.set_avatar_capability("laura", "Not A Slug!", True)
    assert not store.set_avatar_capability("", "google", True)
    assert store.get_avatar_capabilities("laura") == {"notion": True}


def test_capability_default_on_when_connected():
    # UNSET → defaults to the org-level connection state.
    assert store.capability_enabled("laura", "google", connected=True) is True
    assert store.capability_enabled("laura", "google", connected=False) is False
    # explicit OFF overrides a connected default (owner turned it off).
    store.set_avatar_capability("laura", "google", False)
    assert store.capability_enabled("laura", "google", connected=True) is False
    # explicit ON is honoured regardless of the default.
    store.set_avatar_capability("laura", "slack", True)
    assert store.capability_enabled("laura", "slack", connected=False) is True


# ── POST endpoint: owner-only + validation ─────────────────────────────

def test_capability_endpoint_requires_login(client):
    r = client.post("/dashboard/avatar/laura/capability",
                    json={"capability": "google", "enabled": True})
    assert r.status_code == 401  # no cookie → login required


def test_capability_endpoint_owner_sets_flag(client):
    _login(client)
    r = client.post("/dashboard/avatar/laura/capability",
                    json={"capability": "google", "enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["capability"] == "google" and body["enabled"] is False
    assert store.get_avatar_capabilities("laura") == {"google": False}


def test_capability_endpoint_rejects_unknown_capability(client):
    _login(client)
    # Junk-shaped keys are rejected; slug-shaped app keys are accepted (the
    # dashboard only offers slugs the org actually connected via Pipedream).
    r = client.post("/dashboard/avatar/laura/capability",
                    json={"capability": "Not A Slug!", "enabled": True})
    assert r.status_code == 400
    assert store.get_avatar_capabilities("laura") == {}
    r = client.post("/dashboard/avatar/laura/capability",
                    json={"capability": "dropbox", "enabled": True})
    assert r.status_code == 200
    assert store.get_avatar_capabilities("laura") == {"dropbox": True}


def test_capability_endpoint_unknown_avatar_404(client):
    _login(client)
    r = client.post("/dashboard/avatar/nope/capability",
                    json={"capability": "google", "enabled": True})
    assert r.status_code == 404


# ── summary exposes capabilities_toggle ────────────────────────────────

def test_summary_exposes_capabilities_toggle(client):
    body = client.get("/dashboard/summary").json()
    laura = next(a for a in body["avatars"] if a["id"] == "laura")
    ct = laura["capabilities_toggle"]
    for cap in ("google", "slack"):
        assert set(ct[cap].keys()) == {"on", "connected"}
        assert isinstance(ct[cap]["on"], bool)
        assert isinstance(ct[cap]["connected"], bool)


# ── ENFORCEMENT: google gate at approve→execute ────────────────────────

def test_google_action_skipped_when_capability_off(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", avatar_id="laura")
    store.set_avatar_capability("laura", "gmail", False)  # owner turns Gmail OFF (split toggle)
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m1"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is False and body["capability_blocked"] is True
    assert not calls  # the native executor never ran the Google action
    # Google is off for this avatar and this org has NO Slack agent linked:
    # since 2026-07-22 ("Cedric lives inside Slack") that's a track-only
    # approved card — recorded for the humans, nothing dispatches, never a
    # doomed "couldn't complete".
    st = ledger.action_statuses(["a1"], org_id=user["org_id"]).get("a1")
    assert st and st["status"] == "approved"
    assert "tracked only" in st["detail"]


def test_google_action_runs_when_capability_not_off(client, monkeypatch):
    monkeypatch.setattr(settings, "native_executor", True)
    user = _login(client)
    _seed_action(user["org_id"], "a1", avatar_id="laura")
    # google UNSET (default on) → the action executes as before.
    calls: list = []
    _mock_send(monkeypatch, {"ok": True, "message_id": "m-9"}, calls)

    r = client.post("/dashboard/actions/a1/approve")
    body = r.json()
    assert body["executed"] is True and body["capability_blocked"] is False
    assert len(calls) == 1
    assert body["status"]["status"] == "done"


# ── ENFORCEMENT: slack gate at auto-deliver ────────────────────────────

def test_slack_delivery_skipped_when_capability_off(monkeypatch):
    monkeypatch.setattr(settings, "autopilot_deliver", True)
    monkeypatch.setattr(settings, "autopilot_deliver_to", "")
    monkeypatch.setattr(autopilot.actions, "artifact_to_slack_text",
                        lambda name, art: "txt")
    posted: list = []
    monkeypatch.setattr(autopilot.actions, "post_to_slack",
                        lambda text: posted.append(text) or {"sent": True})

    store.set_avatar_capability("laura", "slack", False)
    res = autopilot.maybe_deliver("Laura", {"avatar_id": "laura", "follow_up_email": {}})
    assert res["slack"]["reason"] == "slack capability off"
    assert not posted

    # control: an avatar whose slack is not off still posts.
    res2 = autopilot.maybe_deliver("Cedric", {"avatar_id": "cedric", "follow_up_email": {}})
    assert res2["slack"]["sent"] is True
    assert posted
