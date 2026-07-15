"""Schedule a meeting FROM the dashboard calendar — POST /dashboard/calendar/event.

Key-free: Google's create_calendar_event is mocked (no network), the store is a
fresh temp SQLite, native OAuth is faked via store.set_org_oauth. Covers the
security- and contract-sensitive behaviors:
- Owner-only: an unauthenticated POST is rejected (401), like the other
  dashboard mutations.
- The event is created on the CALLER's OWN org native Google token, and the
  response returns {ok, event_id, html_link}.
- avatar_id resolves the avatar's +tag invite alias and adds it to attendees so
  the avatar auto-joins.
- No native Google token → {ok:false, error:"connect_google"} (never a 500).
- A missing title/start soft-fails; a Google hiccup soft-fails (never raises).
- The native Upcoming path badges an event whose attendees include the alias.
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
from app import auth, google_client, ledger, store  # noqa: E402
from app.config import settings  # noqa: E402

INVITE = "laura.ai.122222@gmail.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    monkeypatch.setattr(settings, "google_token_enc_key", "")  # encrypt w/o config
    monkeypatch.setattr(settings, "session_secret", "test-secret")
    monkeypatch.setattr(settings, "calendar_invite_emails", INVITE)
    monkeypatch.setattr(settings, "default_avatar_id", "laura")
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def _mock_create(monkeypatch, sink: list, result: dict | None = None):
    def fake_create(org_id, event):
        sink.append((org_id, event))
        return result or {"ok": True, "event_id": "evt_1",
                          "event_url": "https://calendar.google.com/e/evt_1",
                          "meet_url": "https://meet.google.com/evt-1abc-def"}
    monkeypatch.setattr(google_client, "create_calendar_event", fake_create)


class _Resp:
    def __init__(self, code: int, payload: dict):
        self.status_code = code
        self._p = payload

    def json(self) -> dict:
        return self._p


def _future_iso(hours: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


# ── owner-only ──────────────────────────────────────────────────────────

def test_create_event_requires_login(client):
    r = client.post("/dashboard/calendar/event",
                    json={"title": "Sync", "start": _future_iso()})
    assert r.status_code == 401  # no cookie → login required


# ── happy path: creates on the caller's own org native Google ───────────

def test_create_event_owner_creates(client, monkeypatch):
    user = _login(client)
    store.set_org_oauth(user["org_id"], "rt-user", email="me@example.com")
    calls: list = []
    _mock_create(monkeypatch, calls)

    r = client.post("/dashboard/calendar/event",
                    json={"title": "Board sync", "start": _future_iso(),
                          "duration_min": 45})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["event_id"] == "evt_1"
    assert body["html_link"].endswith("/evt_1")
    assert body["avatar_added"] is False
    # created on the caller's OWN org, with title + start + a derived end.
    assert len(calls) == 1
    org, event = calls[0]
    assert org == user["org_id"]
    assert event["title"] == "Board sync"
    assert event["start"] and event["end"]
    assert "attendees" not in event  # no avatar / explicit attendees
    # the endpoint surfaces the Meet link the executor returns.
    assert body["meet_url"] == "https://meet.google.com/evt-1abc-def"


# ── scheduled event provisions a real Google Meet link (FIX A) ──────────

def test_create_event_provisions_google_meet(client, monkeypatch):
    """End-to-end (only httpx mocked, the REAL create_calendar_event runs): the
    outgoing events.insert asks Google for a Meet conference (conferenceData
    .createRequest + conferenceDataVersion=1) and the endpoint returns the
    resulting hangoutLink as meet_url."""
    user = _login(client)
    store.set_org_oauth(user["org_id"], "rt-user", email="me@example.com")
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")

    captured: dict = {}

    def fake_post(url, **kw):
        if url.endswith("/token"):
            return _Resp(200, {"access_token": "at-1"})
        if "calendar" in url:
            captured["params"] = kw.get("params")
            captured["json"] = kw.get("json")
            return _Resp(200, {
                "id": "evt_meet",
                "htmlLink": "https://calendar.google.com/e/evt_meet",
                "hangoutLink": "https://meet.google.com/xyz-abcd-efg",
            })
        return _Resp(404, {})

    monkeypatch.setattr(google_client.httpx, "post", fake_post)

    r = client.post("/dashboard/calendar/event",
                    json={"title": "Board sync", "start": _future_iso(),
                          "duration_min": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # the mocked hangoutLink flows back to the UI as meet_url.
    assert body["meet_url"] == "https://meet.google.com/xyz-abcd-efg"
    # conferenceDataVersion=1 is REQUIRED for Google to honour the createRequest.
    assert captured["params"]["conferenceDataVersion"] == 1
    # the outgoing body asks for a hangoutsMeet conference with a unique id.
    cr = captured["json"]["conferenceData"]["createRequest"]
    assert cr["requestId"]
    assert cr["conferenceSolutionKey"]["type"] == "hangoutsMeet"


# ── avatar_id adds the +tag invite alias so it auto-joins ───────────────

def test_create_event_adds_avatar_invite(client, monkeypatch):
    user = _login(client)
    store.set_org_oauth(user["org_id"], "rt-user", email="me@example.com")
    calls: list = []
    _mock_create(monkeypatch, calls)

    r = client.post("/dashboard/calendar/event",
                    json={"title": "Demo", "start": _future_iso(),
                          "duration_min": 30, "avatar_id": "cedric"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["avatar_added"] is True
    _, event = calls[0]
    # Cedric is a +tag alias of the watched inbox (the default avatar is bare).
    assert "laura.ai.122222+cedric@gmail.com" in event["attendees"]


def test_create_event_default_avatar_uses_bare_alias(client, monkeypatch):
    user = _login(client)
    store.set_org_oauth(user["org_id"], "rt-user", email="me@example.com")
    calls: list = []
    _mock_create(monkeypatch, calls)

    client.post("/dashboard/calendar/event",
                json={"title": "1:1", "start": _future_iso(), "avatar_id": "laura"})
    _, event = calls[0]
    assert "laura.ai.122222@gmail.com" in event["attendees"]  # bare, no +tag


def test_create_event_unknown_avatar_soft_fails(client, monkeypatch):
    user = _login(client)
    store.set_org_oauth(user["org_id"], "rt-user", email="me@example.com")
    calls: list = []
    _mock_create(monkeypatch, calls)
    r = client.post("/dashboard/calendar/event",
                    json={"title": "X", "start": _future_iso(), "avatar_id": "ghost"})
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "unknown avatar_id"}
    assert not calls  # never reached Google


# ── no native token → connect_google (never a 500) ──────────────────────

def test_create_event_without_google_returns_connect(client, monkeypatch):
    _login(client)  # logged in, but this org has no native token
    calls: list = []
    _mock_create(monkeypatch, calls)
    r = client.post("/dashboard/calendar/event",
                    json={"title": "Sync", "start": _future_iso()})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "connect_google"}
    assert not calls


# ── validation + soft-fail on a Google hiccup ───────────────────────────

def test_create_event_missing_title_soft_fails(client, monkeypatch):
    user = _login(client)
    store.set_org_oauth(user["org_id"], "rt-user", email="me@example.com")
    r = client.post("/dashboard/calendar/event", json={"start": _future_iso()})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_create_event_google_error_soft_fails(client, monkeypatch):
    user = _login(client)
    store.set_org_oauth(user["org_id"], "rt-user", email="me@example.com")
    calls: list = []
    _mock_create(monkeypatch, calls,
                 result={"ok": False, "error": "calendar insert failed (HTTP 500)"})
    r = client.post("/dashboard/calendar/event",
                    json={"title": "Sync", "start": _future_iso()})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "calendar insert failed" in body["error"]


# ── native Upcoming badges an event that has the avatar alias ───────────

def test_upcoming_badges_avatar_from_invite_alias(client, monkeypatch):
    user = _login(client)
    store.set_org_oauth(user["org_id"], "rt-user", email="me@example.com")
    ev = {
        "id": "e1", "summary": "Demo w/ Acme", "status": "confirmed",
        "start": {"dateTime": _future_iso()}, "end": {"dateTime": _future_iso(3)},
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        # the avatar is invited via its +tag alias → auto-join badge.
        "attendees": [{"email": "laura.ai.122222+cedric@gmail.com"},
                      {"email": "someone@acme.com"}],
    }
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=25: {"ok": True, "events": [ev]},
    )
    m = client.get("/dashboard/upcoming").json()["meetings"][0]
    assert m["auto_join"] is True
    assert m["auto_join_avatar"] == "cedric"
