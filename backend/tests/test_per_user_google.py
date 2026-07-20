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
import app.api.oauth as _oauth_api  # noqa: E402  (oauth routes/consts extracted from main)
from app import auth, ledger, store  # noqa: E402
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
    monkeypatch.setattr(settings, "laura_api_token", "")  # no machine bearer in test
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


def _connect_state(client) -> str:
    """Drive /oauth/google/connect for THIS browser and return the signed `state`
    it puts in the Google auth URL. The matching single-use nonce cookie is set
    on the TestClient's cookie jar automatically, so a follow-up callback with
    this state is correctly browser-bound."""
    from urllib.parse import parse_qs, urlparse

    r = client.get("/oauth/google/connect", follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def test_callback_accepts_non_avatar_account_and_skips_recall(client, monkeypatch):
    """A normal user connecting their OWN Google is accepted (no more "Wrong
    account"), the token lands on THEIR org, and the avatar-only Recall
    auto-join step is skipped."""
    calls = _wire_callback(
        monkeypatch, oauth_email="alice@example.com", invite_filter=AVATAR_EMAIL
    )
    state = _connect_state(client)
    resp = client.get(
        "/oauth/google/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )

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
    state = _connect_state(client)
    resp = client.get(
        "/oauth/google/callback",
        params={"code": "abc", "state": state},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert store.get_org_oauth("u_connecting")  # token persisted too
    assert len(calls) == 1  # Recall calendar created for the avatar inbox
    assert calls[0]["oauth_email"] == AVATAR_EMAIL


def test_callback_invalid_state_still_rejected(client, monkeypatch):
    """Relaxing the ACCOUNT lock must not weaken the CSRF state check. A callback
    whose `state` isn't the signed nonce this browser was issued at /connect is
    rejected (here: no /connect ran, so there is no bound cookie at all)."""
    _wire_callback(monkeypatch, oauth_email="alice@example.com", invite_filter=AVATAR_EMAIL)
    resp = client.get(
        "/oauth/google/callback",
        params={"code": "abc", "state": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "state" in resp.json()["error"].lower()
    # A rejected callback must never have stored a token.
    assert store.get_org_oauth("u_connecting") is None


def test_callback_rejects_state_not_bound_to_this_browser(client, monkeypatch):
    """CSRF per-browser binding: a VALIDLY-SIGNED state that belongs to a
    DIFFERENT flow (a different nonce than this browser's connect cookie) is
    rejected — a signature alone is not enough, it must match the cookie set on
    THIS browser. This is the login-CSRF / refresh-token-injection defense."""
    _wire_callback(monkeypatch, oauth_email="alice@example.com", invite_filter=AVATAR_EMAIL)
    # This browser starts a real flow → its nonce cookie is now in the jar.
    _connect_state(client)
    # An attacker crafts their OWN correctly-signed state (fresh, different nonce)
    # and tries to replay it into the victim's browser.
    _, attacker_state = main_module.auth.issue_oauth_state(
        _oauth_api.CALENDAR_STATE_PURPOSE
    )
    resp = client.get(
        "/oauth/google/callback",
        params={"code": "abc", "state": attacker_state},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "state" in resp.json()["error"].lower()
    assert store.get_org_oauth("u_connecting") is None


def test_connect_requires_login_when_enabled(client, monkeypatch):
    """Login-enabled deployment: an UNAUTHENTICATED /oauth/google/connect is
    rejected (401), so an anonymous browser can't even start the flow."""
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")  # auth.enabled()
    monkeypatch.setattr(settings, "laura_api_token", "")
    monkeypatch.setattr(main_module.auth, "current_user", lambda request: None)
    resp = client.get("/oauth/google/connect", follow_redirects=False)
    assert resp.status_code == 401


def test_callback_login_enabled_unauth_not_stored_on_demo_org(client, monkeypatch):
    """Login-enabled deployment: an UNAUTHENTICATED callback is rejected (401)
    and NOTHING is stored on the shared demo/owner org — the old anonymous
    fallback to settings.demo_org_id is closed."""
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csec")  # auth.enabled()
    monkeypatch.setattr(settings, "laura_api_token", "")
    monkeypatch.setattr(main_module.httpx, "AsyncClient", _fake_async_client("mallory@evil.com"))
    monkeypatch.setattr(main_module.auth, "current_user", lambda request: None)
    resp = client.get(
        "/oauth/google/callback",
        params={"code": "abc", "state": "anything"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert store.get_org_oauth(settings.demo_org_id) is None


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
        "_laura_calendar": {
            "name": "Customer calls", "color": "#d50000", "primary": False,
        },
    }
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=25, **kw: {"ok": True, "events": [ev]},
    )

    j = client.get("/dashboard/upcoming").json()
    assert j["calendar"]["source"] == "google"
    assert j["calendar"]["email"] == "me@example.com"
    m = j["meetings"][0]
    assert m["title"] == "Board sync"
    assert m["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert m["platform"] == "Google Meet"
    assert m["attendees"] == 2
    assert m["calendar_name"] == "Customer calls"
    assert m["calendar_color"] == "#d50000"
    assert "@" not in m["calendar_name"]  # raw calendar id/email is not exposed
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
        lambda org, max_results=25, **kw: {"ok": True, "events": events},
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


# ── shared-org cross-tenant isolation (the Duccio/Ananth leak) ──

def _login(client, email: str) -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def test_upcoming_never_leaks_a_colleagues_calendar(client, monkeypatch):
    """Two members of the SAME shared org (a verified corporate domain maps both
    onto one org_id) must each see ONLY their own calendar. Reproduces the real
    leak: one person connected Google and a co-worker saw THEIR calendar.

    Duccio (alice@sffstudio.com) connects — dual-write: user_oauth[duccio] +
    org_oauth[org_sff], both his Google email. Ananth (bob@sffstudio.com) logs in
    to the SAME org and must NOT see Duccio's calendar; Duccio still sees his.

    Shared orgs only form under the parked flag now (personal-first default,
    2026-07-16) — enable it so this per-user isolation invariant stays covered
    for when teams ship. (Under the default the two are in different orgs, so
    the leak is impossible by construction; per-user isolation WITHIN a shared
    org is the harder property this pins.)"""
    monkeypatch.setattr(settings, "shared_domain_orgs", True)
    duccio = store.upsert_user("alice@sffstudio.com")
    ananth = store.upsert_user("bob@sffstudio.com")
    assert duccio["org_id"] == ananth["org_id"] == "org_sff"  # shared org
    assert duccio["user_id"] != ananth["user_id"]

    # Duccio connects Google (mirrors the /oauth/google/callback dual-write).
    store.set_user_oauth(duccio["user_id"], "rt-duccio", email="duccio@sffstudio.com")
    store.set_org_oauth("org_sff", "rt-duccio", email="duccio@sffstudio.com")

    from app import google_client
    ev = {
        "id": "e1", "summary": "Duccio 1:1", "status": "confirmed",
        "start": {"dateTime": _future_iso()}, "end": {"dateTime": _future_iso(3)},
        "hangoutLink": "https://meet.google.com/pri-vate-cal",
        "_laura_calendar": {
            "id": "primary", "name": "Personal",
            "color": "#4285f4", "primary": True,
        },
    }
    seen_principals: list = []

    def _fake_list(org, max_results=25, *, oauth=None, principal="", on_rotate=None, **kw):
        seen_principals.append(principal)
        return {"ok": True, "events": [ev]}

    monkeypatch.setattr(google_client, "list_calendar_events", _fake_list)

    # Ananth (colleague, no calendar of his own) must NOT see Duccio's — and the
    # calendar fetch must never even be reached with a foreign token.
    _login(client, "bob@sffstudio.com")
    j = client.get("/dashboard/upcoming").json()
    assert j["calendar"]["connected"] is False
    assert j["calendar"].get("connect_url") == "/oauth/google/connect"
    assert j["meetings"] == []
    assert seen_principals == []  # no calendar read happened for the colleague

    # Duccio sees his OWN calendar, fetched with his per-user principal.
    _login(client, "alice@sffstudio.com")
    j = client.get("/dashboard/upcoming").json()
    assert j["calendar"]["source"] == "google"
    assert j["calendar"]["email"] == "duccio@sffstudio.com"
    assert j["meetings"][0]["title"] == "Duccio 1:1"
    event_ref = j["meetings"][0]["event_ref"]
    assert event_ref and "primary" not in event_ref  # opaque, not a raw calendar id
    from app import dashboard
    assert dashboard._read_calendar_event_ref(event_ref) == (
        duccio["user_id"], "primary", "e1"
    )
    assert seen_principals == [f"user:{duccio['user_id']}"]


def test_create_event_never_writes_to_a_colleagues_calendar(client, monkeypatch):
    """Write-side of the same leak: a shared-org colleague scheduling from the
    dashboard must NOT create the event on the connector's calendar."""
    duccio = store.upsert_user("alice@sffstudio.com")
    store.upsert_user("bob@sffstudio.com")
    store.set_user_oauth(duccio["user_id"], "rt-duccio", email="duccio@sffstudio.com")
    store.set_org_oauth("org_sff", "rt-duccio", email="duccio@sffstudio.com")

    from app import google_client
    created: list = []
    monkeypatch.setattr(
        google_client, "create_calendar_event",
        lambda org, event, **kw: created.append((org, event, kw)) or {"ok": True},
    )

    _login(client, "bob@sffstudio.com")
    r = client.post("/dashboard/calendar/event",
                    json={"title": "sneaky", "start": _future_iso()})
    assert r.json().get("error") == "connect_google"
    assert created == []  # nothing created on Duccio's calendar


def test_add_avatar_to_existing_event_uses_callers_google(client, monkeypatch):
    user = _login(client, "alice@sffstudio.com")
    store.set_user_oauth(user["user_id"], "rt-alice", email="alice@sffstudio.com")
    from app import dashboard, google_client

    monkeypatch.setattr(
        dashboard, "_avatar_email", lambda avatar_id: f"laura+{avatar_id}@example.com"
    )
    seen = {}

    def fake_add(org, calendar_id, event_id, attendee_email, **kwargs):
        seen.update({
            "org": org, "calendar_id": calendar_id, "event_id": event_id,
            "attendee_email": attendee_email, **kwargs,
        })
        return {"ok": True, "event_id": event_id, "idempotent": False}

    monkeypatch.setattr(google_client, "add_calendar_event_attendee", fake_add)
    event_ref = dashboard._calendar_event_ref(
        user["user_id"], "team@group.calendar.google.com", "event-1"
    )
    response = client.post(
        "/dashboard/calendar/event/avatar",
        json={"event_ref": event_ref, "avatar_id": "laura"},
    )
    assert response.status_code == 200 and response.json()["ok"] is True
    assert seen["calendar_id"] == "team@group.calendar.google.com"
    assert seen["event_id"] == "event-1"
    assert seen["attendee_email"] == "laura+laura@example.com"
    assert seen["principal"] == f"user:{user['user_id']}"
    assert seen["oauth"]["email"] == "alice@sffstudio.com"


def test_event_ref_cannot_be_replayed_by_another_user(client):
    alice = _login(client, "alice@sffstudio.com")
    from app import dashboard

    event_ref = dashboard._calendar_event_ref(alice["user_id"], "primary", "event-1")
    _login(client, "bob@sffstudio.com")
    response = client.post(
        "/dashboard/calendar/event/avatar",
        json={"event_ref": event_ref, "avatar_id": "laura"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_event_ref"


def test_disconnect_clears_the_per_user_token_too(client):
    """Disconnect must revoke the credential the dashboard actually reads —
    the PER-USER row — not just the org row (else the button silently no-ops
    and the calendar stays readable after 'disconnecting')."""
    user = _login(client, "alice@sffstudio.com")
    store.set_user_oauth(user["user_id"], "rt-x", email="duccio@sffstudio.com")
    store.set_org_oauth(user["org_id"], "rt-x", email="duccio@sffstudio.com")

    r = client.post("/oauth/google/disconnect")
    assert r.status_code == 200
    assert r.json()["cleared"] is True
    assert store.get_user_oauth(user["user_id"]) is None
    assert store.get_org_oauth(user["org_id"]) is None
    # And the dashboard agrees: back to the "connect your Google" state.
    j = client.get("/dashboard/upcoming").json()
    assert j["calendar"]["connected"] is False
