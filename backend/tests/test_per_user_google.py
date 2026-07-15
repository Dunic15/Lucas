"""Per-user native Google — the /oauth/google/callback relaxation and the
Upcoming endpoint preferring the caller's OWN calendar.

Key-free: Google is faked (async httpx stub), Recall's create_calendar is a spy,
and the store is a fresh temp SQLite. Covers the security-sensitive behaviors:
- ANY account may now connect (the old "Wrong Google account" reject is gone),
  and the refresh token lands on the CONNECTING user's own org.
- A non-avatar account SKIPS the avatar-only Recall calendar auto-join step; the
  avatar's own account still creates it (auto-join intact).
- /dashboard/upcoming reads the caller's OWN Google calendar when a native token
  exists, else falls back to the Recall Calendar V2 path.
"""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from app import ledger, store  # noqa: E402
from app.config import settings  # noqa: E402

AVATAR_EMAIL = "laura.ai.122222@gmail.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    # Encrypt per-org tokens at rest without extra config in tests.
    monkeypatch.setattr(settings, "google_token_enc_key", "")
    monkeypatch.setattr(settings, "session_secret", "test-secret")
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file
    return TestClient(main_module.app)


# ── OAuth callback: fake Google (async httpx) ──

class _Resp:
    def __init__(self, payload: dict, status: int = 200):
        self._p = payload
        self.status_code = status

    def json(self) -> dict:
        return self._p

    def raise_for_status(self) -> None:
        return None


def _fake_async_client(email: str, refresh: str = "rt-user", access: str = "at-user"):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            return _Resp({"refresh_token": refresh, "access_token": access})

        async def get(self, url, **kw):
            return _Resp({"email": email})

    return _Client


def _wire_callback(monkeypatch, *, oauth_email: str, invite_filter: str):
    """Configure the callback env + fakes; returns the create_calendar spy list."""
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")
    monkeypatch.setattr(settings, "calendar_invite_emails", invite_filter)
    monkeypatch.setattr(settings, "calendar_oauth_state", "")  # no state gate in test
    monkeypatch.setattr(main_module.httpx, "AsyncClient", _fake_async_client(oauth_email))
    # The connecting user's org comes ONLY from the session cookie resolver.
    monkeypatch.setattr(
        main_module.auth, "current_user",
        lambda request: {"org_id": "u_connecting", "user_id": "usr-1"},
    )
    calls: list[dict] = []

    def fake_create_calendar(**kw):
        calls.append(kw)
        return {"id": "cal_1"}

    monkeypatch.setattr(main_module.recall_client, "create_calendar", fake_create_calendar)
    return calls


def test_callback_accepts_non_avatar_account_and_skips_recall(client, monkeypatch):
    """A normal user connecting their OWN Google is accepted (no more "Wrong
    account"), the token lands on THEIR org, and the avatar-only Recall
    auto-join step is skipped."""
    calls = _wire_callback(
        monkeypatch, oauth_email="alice@example.com", invite_filter=AVATAR_EMAIL
    )
    resp = client.get("/oauth/google/callback?code=abc&state=", follow_redirects=False)

    assert resp.status_code == 302
    assert "/dashboard?google=connected" in resp.headers["location"]
    # Token stored on the CONNECTING user's org — never the avatar's, never global.
    got = store.get_org_oauth("u_connecting")
    assert got and got["refresh_token"] == "rt-user"
    assert got["email"] == "alice@example.com"
    # Avatar-only Recall calendar auto-join must NOT run for a normal user.
    assert calls == []


def test_callback_avatar_account_still_creates_recall_calendar(client, monkeypatch):
    """The avatar's own account keeps the Recall calendar auto-join behavior."""
    calls = _wire_callback(
        monkeypatch, oauth_email=AVATAR_EMAIL, invite_filter=AVATAR_EMAIL
    )
    resp = client.get("/oauth/google/callback?code=abc&state=", follow_redirects=False)

    assert resp.status_code == 302
    assert store.get_org_oauth("u_connecting")  # token persisted too
    assert len(calls) == 1  # Recall calendar created for the avatar inbox
    assert calls[0]["oauth_email"] == AVATAR_EMAIL


def test_callback_invalid_state_still_rejected(client, monkeypatch):
    """Relaxing the ACCOUNT lock must not weaken the CSRF state check."""
    _wire_callback(monkeypatch, oauth_email="alice@example.com", invite_filter=AVATAR_EMAIL)
    monkeypatch.setattr(settings, "calendar_oauth_state", "expected-state")
    resp = client.get("/oauth/google/callback?code=abc&state=wrong", follow_redirects=False)
    assert resp.status_code == 400
    assert "state" in resp.json()["error"].lower()


# ── /dashboard/upcoming: native vs Recall selection ──

def _future_iso(hours: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_upcoming_prefers_native_google_calendar(client, monkeypatch):
    """When the caller's org has a native token, Upcoming is built from THEIR own
    Google calendar (source=google) with the meeting URL for dispatch."""
    store.set_org_oauth(settings.demo_org_id, "rt-x", email="me@example.com")
    from app import google_client

    ev = {
        "id": "e1",
        "summary": "Board sync",
        "status": "confirmed",
        "start": {"dateTime": _future_iso()},
        "end": {"dateTime": _future_iso(3)},
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "attendees": [{"email": "a@b.com"}, {"email": "c@d.com"}],
    }
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=25: {"ok": True, "events": [ev]},
    )

    j = client.get("/dashboard/upcoming").json()
    assert j["calendar"]["source"] == "google"
    assert j["calendar"]["email"] == "me@example.com"
    m = j["meetings"][0]
    assert m["title"] == "Board sync"
    assert m["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert m["platform"] == "Google Meet"
    assert m["attendees"] == 2
    assert m["auto_join"] is False  # nothing dispatched yet


def test_upcoming_native_parses_conference_entry_point(client, monkeypatch):
    """Meeting URL falls back to a video conferenceData entry point (no
    hangoutLink), and a URL-shaped location is honored too."""
    store.set_org_oauth(settings.demo_org_id, "rt-x", email="me@example.com")
    from app import google_client

    events = [
        {
            "id": "e1", "summary": "Zoom call", "status": "confirmed",
            "start": {"dateTime": _future_iso()}, "end": {"dateTime": _future_iso(3)},
            "conferenceData": {"entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1"},
                {"entryPointType": "video", "uri": "https://us02web.zoom.us/j/123"},
            ]},
        },
        {
            "id": "e2", "summary": "No link", "status": "confirmed",
            "start": {"dateTime": _future_iso(1)}, "end": {"dateTime": _future_iso(2)},
            "location": "Room 4B",
        },
    ]
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=25: {"ok": True, "events": events},
    )

    rows = {m["id"]: m for m in client.get("/dashboard/upcoming").json()["meetings"]}
    assert rows["e1"]["meeting_url"] == "https://us02web.zoom.us/j/123"
    assert rows["e1"]["platform"] == "Zoom"
    assert rows["e2"]["meeting_url"] == ""  # non-URL location → no dispatch link
    assert rows["e2"]["has_link"] is False


def test_upcoming_falls_back_to_recall_without_native_token(client, monkeypatch):
    """No native token → the existing Recall Calendar V2 path is used."""
    from app import recall_client

    monkeypatch.setattr(recall_client, "list_calendars", lambda: [])
    j = client.get("/dashboard/upcoming").json()
    assert j["calendar"]["connected"] is False
    assert j["calendar"].get("source") != "google"
