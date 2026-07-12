"""Dashboard login (auth.py) — cookie signing, Google callback, org scoping.

Key-free like the rest of the suite. Google's token endpoint is mocked; no
network. The properties under test: the demo stays open when login isn't
configured, cookies can't be forged or outlive their expiry, the callback
creates a user and scopes the dashboard, and one org can never see (or end)
another org's rows.
"""
from __future__ import annotations

import importlib
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, cedric, ledger, store
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file — needs its tables too
    return TestClient(main_module.app)


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid-test")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csecret-test")


def _login(client, email, name="Test User") -> dict:
    """Create a user + set its session cookie on the client. Returns the user."""
    user = store.upsert_user(email=email, name=name)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


# ── cookie mechanics ───────────────────────────────────────────────────

def test_cookie_roundtrip():
    assert auth.read_cookie(auth.make_cookie("u_abc")) == "u_abc"


def test_cookie_tamper_rejected():
    good = auth.make_cookie("u_abc")
    payload, sig = good.rsplit(".", 1)
    assert auth.read_cookie(payload + "." + "0" * len(sig)) is None
    assert auth.read_cookie(payload) is None
    assert auth.read_cookie("") is None


def test_cookie_expiry_rejected():
    assert auth.read_cookie(auth.make_cookie("u_abc", ttl=-5)) is None


def test_user_id_is_deterministic_from_email():
    a = store.user_id_for_email("Duccio@Example.com ")
    b = store.user_id_for_email("duccio@example.com")
    assert a == b and a.startswith("u_")


def test_upsert_user_marks_only_first_login_created(client):
    first = store.upsert_user("first@example.com")
    second = store.upsert_user("first@example.com")
    assert first["created"] is True
    assert second["created"] is False


# ── demo mode preserved ────────────────────────────────────────────────

def test_summary_open_when_auth_not_configured(client):
    assert client.get("/dashboard/summary").status_code == 200


def test_login_page_served(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Continue with Google" in resp.text


# ── login required when configured ─────────────────────────────────────

def test_summary_requires_login_when_auth_enabled(client, google_on):
    resp = client.get("/dashboard/summary")
    assert resp.status_code == 401
    assert resp.json()["error"] == "login_required"


def test_summary_with_cookie_when_auth_enabled(client, google_on):
    user = _login(client, "owner@example.com")
    data = client.get("/dashboard/summary").json()
    assert data["user"]["email"] == "owner@example.com"
    assert data["auth_enabled"] is True
    assert user["org_id"] == user["user_id"]


def test_meetings_list_requires_login_when_auth_enabled(client, google_on):
    # Transcripts are PII — the meetings archive must not be readable anonymously.
    resp = client.get("/meetings/list")
    assert resp.status_code == 401
    assert resp.json()["error"] == "login_required"


def test_meetings_list_open_when_auth_disabled(client):
    # Demo mode (no Google configured) stays open + key-free, like /dashboard.
    store.save_artifact("bot-demo", {"summary": "s", "transcript": "t"})
    resp = client.get("/meetings/list")
    assert resp.status_code == 200
    assert any(m["bot_id"] == "bot-demo" for m in resp.json()["meetings"])


def test_meetings_list_scopes_to_caller_org(client, google_on):
    user = _login(client, "owner@example.com")
    org = user["org_id"]
    store.save_artifact("mine", {"summary": "a", "transcript": "x", "org_id": org})
    store.save_artifact("theirs", {"summary": "b", "transcript": "y", "org_id": "other-org"})
    store.save_artifact("unowned", {"summary": "c", "transcript": "z"})  # empty org_id
    bots = {m["bot_id"] for m in client.get("/meetings/list").json()["meetings"]}
    assert "mine" in bots
    assert "unowned" in bots       # legacy/unowned stays visible (redeliver rule)
    assert "theirs" not in bots    # another org's transcript never leaks


def test_gmail_status_requires_login_when_auth_enabled(client, google_on):
    # recent_joins leaks joinable Meet URLs + bot_ids — must not be anonymous.
    assert client.get("/gmail/status").status_code == 401


def test_live_token_requires_login_when_auth_enabled(client, google_on):
    # Minting an Anam token bills per-minute — no anonymous minting in prod.
    assert client.post("/live/token", json={"avatar_id": "laura"}).status_code == 401


def test_bearer_still_works_when_auth_enabled(client, google_on, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    ok = client.get(
        "/dashboard/summary", headers={"Authorization": "Bearer sesame"}
    )
    assert ok.status_code == 200


# ── google callback (mocked exchange) ──────────────────────────────────

def test_google_callback_creates_user_and_sets_cookie(
    client, google_on, monkeypatch
):
    import base64, json as _json

    claims = {
        "iss": "https://accounts.google.com",
        "aud": "cid-test",
        "exp": time.time() + 600,
        "email": "New.Person@Example.com",
        "email_verified": True,
        "name": "New Person",
    }
    body = base64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=")
    fake_id_token = f"h.{body}.s"

    class FakeResp:
        status_code = 200

        def json(self):
            return {"id_token": fake_id_token}

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return FakeResp()

    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    preprovisioned = threading.Event()
    provision_args: list[tuple] = []

    def fake_provision(*args):
        provision_args.append(args)
        preprovisioned.set()
        return None

    monkeypatch.setattr(cedric, "provision_org", fake_provision)

    state = _valid_state(client, nonce="nonce-abc")
    resp = client.get(
        "/auth/google/callback",
        params={"code": "fake-code", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/dashboard"
    assert auth.COOKIE_NAME in resp.cookies

    uid = auth.read_cookie(resp.cookies[auth.COOKIE_NAME])
    user = store.get_user(uid)
    assert user["email"] == "new.person@example.com"
    assert preprovisioned.wait(1), "first login should run pending org provisioning"
    assert provision_args == [(uid, None, "", "cedric")]

    # The endpoint is idempotent and every login retries it. A transient Cedric
    # failure on signup therefore self-heals without deleting the Laura user or
    # requiring an operator to replay provisioning.
    preprovisioned.clear()
    retry_state = _valid_state(client, nonce="nonce-retry")
    retry = client.get(
        "/auth/google/callback",
        params={"code": "fake-code", "state": retry_state},
        follow_redirects=False,
    )
    assert retry.status_code == 302
    assert preprovisioned.wait(1), "repeat login should retry pending provisioning"
    assert provision_args == [
        (uid, None, "", "cedric"),
        (uid, None, "", "cedric"),
    ]


def _valid_state(client, nonce="nonce-abc", exp_offset=600) -> str:
    """Build a browser-bound signed state: set the state cookie on the client
    and return the matching signed state param."""
    payload = auth._b64(
        __import__("json").dumps({"n": nonce, "exp": time.time() + exp_offset}).encode()
    )
    client.cookies.set(auth.STATE_COOKIE, nonce)
    return f"{payload}.{auth._sign(payload, 'state')}"


def test_google_callback_rejects_forged_state(client, google_on):
    resp = client.get(
        "/auth/google/callback",
        params={"code": "x", "state": "forged.deadbeef"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=state" in resp.headers["location"]


def test_callback_rejects_state_without_matching_cookie(client, google_on):
    """A validly-signed state with NO matching browser cookie (login CSRF /
    session fixation) is rejected."""
    payload = auth._b64(
        __import__("json").dumps({"n": "attacker", "exp": time.time() + 600}).encode()
    )
    signed = f"{payload}.{auth._sign(payload, 'state')}"
    # no client.cookies.set(STATE_COOKIE) — the victim never started this flow
    resp = client.get(
        "/auth/google/callback",
        params={"code": "x", "state": signed},
        follow_redirects=False,
    )
    assert "error=state" in resp.headers["location"]


def test_callback_rejects_expired_state(client, google_on):
    state = _valid_state(client, nonce="n1", exp_offset=-5)
    resp = client.get(
        "/auth/google/callback",
        params={"code": "x", "state": state},
        follow_redirects=False,
    )
    assert "error=expired" in resp.headers["location"]


def test_state_and_session_signatures_are_domain_separated():
    """A cookie-purpose signature must not verify as a state-purpose one."""
    payload = auth._b64(b'{"x":1}')
    assert not auth._verify(payload, auth._sign(payload, "session"), "state")
    assert auth._verify(payload, auth._sign(payload, "state"), "state")


def test_id_token_wrong_audience_rejected(google_on):
    import base64, json as _json

    claims = {
        "iss": "https://accounts.google.com",
        "aud": "someone-else",
        "exp": time.time() + 600,
        "email": "a@b.c",
        "email_verified": True,
    }
    body = base64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=")
    assert auth._decode_id_token(f"h.{body}.s") is None


# ── org scoping ────────────────────────────────────────────────────────

def _artifact(org_id: str, summary: str) -> dict:
    return {
        "summary": summary,
        "actions": [],
        "readiness_score": 80,
        "follow_up_email": {},
        "avatar_id": "laura",
        "org_id": org_id,
        "meeting_url": "https://meet.google.com/x",
    }


def test_org_scoping_on_meetings(client, google_on):
    alice = _login(client, "alice@example.com")
    store.save_artifact("bot_alice", _artifact(alice["org_id"], "alice meeting"))
    bob = store.upsert_user(email="bob@example.com")
    store.save_artifact("bot_bob", _artifact(bob["org_id"], "bob meeting"))
    store.save_artifact("bot_shared", _artifact("", "legacy shared meeting"))

    data = client.get("/dashboard/summary").json()
    summaries = {m["summary"] for m in data["meetings"]}
    assert "alice meeting" in summaries
    assert "legacy shared meeting" in summaries  # single-tenant migration mode
    assert "bob meeting" not in summaries


def test_org_scoping_on_live_sessions(client, google_on):
    alice = _login(client, "alice@example.com")
    store.create("bot_a", "https://meet.google.com/a", "laura", org_id=alice["org_id"])
    other = store.upsert_user(email="eve@example.com")
    store.create("bot_e", "https://meet.google.com/e", "laura", org_id=other["org_id"])
    try:
        live = client.get("/dashboard/summary").json()["live"]
        assert {s["bot_id"] for s in live} == {"bot_a"}
    finally:
        store.remove("bot_a")
        store.remove("bot_e")


def test_cannot_end_another_orgs_session(client, google_on):
    _login(client, "alice@example.com")
    eve = store.upsert_user(email="eve@example.com")
    store.create("bot_eve", "https://meet.google.com/e2", "laura", org_id=eve["org_id"])
    try:
        resp = client.post("/sessions/bot_eve/end")
        assert resp.status_code == 403
    finally:
        store.remove("bot_eve")


def test_session_start_stamps_org(client, google_on, monkeypatch):
    """A logged-in dispatch stamps the user's org on the session (stub Recall)."""
    from app import recall_client

    monkeypatch.setattr(recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(
        recall_client, "create_bot", lambda *a, **k: {"id": "bot_stamped"}
    )
    alice = _login(client, "alice@example.com")
    resp = client.post(
        "/sessions/start", json={"meeting_url": "https://meet.google.com/stamp"}
    )
    assert resp.status_code == 200, resp.text
    try:
        assert store.get("bot_stamped").org_id == alice["org_id"]
    finally:
        store.remove("bot_stamped")


def test_logout_clears_cookie(client, google_on):
    _login(client, "alice@example.com")
    resp = client.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 302
    client.cookies.clear()
    assert client.get("/dashboard/summary").status_code == 401


def test_logout_rejects_cross_site(client, google_on):
    _login(client, "alice@example.com")
    resp = client.post(
        "/auth/logout",
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ── review fixes: gate on write endpoints, allowlist, robustness ───────

def test_anonymous_cannot_start_when_login_enabled(client, google_on):
    """The HIGH finding: login on, no bearer token -> anonymous must NOT be able
    to dispatch a per-minute bot."""
    resp = client.post(
        "/sessions/start", json={"meeting_url": "https://meet.google.com/x"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "login_required"


def test_anonymous_cannot_end_when_login_enabled(client, google_on):
    """Anonymous force-end (DoS + meter) is blocked, and the owned session is
    left running."""
    eve = store.upsert_user(email="eve@example.com")
    store.create("bot_prot", "https://meet.google.com/p", "laura", org_id=eve["org_id"])
    try:
        resp = client.post("/sessions/bot_prot/end")
        assert resp.status_code == 401
        assert store.get("bot_prot") is not None  # not finalized
    finally:
        store.remove("bot_prot")


def test_token_plus_login_anonymous_gets_google_gate(client, google_on, monkeypatch):
    """The MED finding: with BOTH a token and Google login, an anonymous browser
    must be told to sign in (auth_enabled True), not to paste a token."""
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    resp = client.get("/dashboard/summary")
    assert resp.status_code == 401
    assert resp.json().get("auth_enabled") is True


def test_logged_in_user_can_end_unowned_session(client, google_on, monkeypatch):
    """A user may end a legacy/service (org_id == '') session — the by-design
    branch the original tests never exercised."""
    from app import cedric, recall_client

    _login(client, "alice@example.com")
    store.create("bot_unowned", "https://meet.google.com/u", "laura", org_id="")
    monkeypatch.setattr(cedric, "wire_artifact", lambda a: a)
    # /end finalizes the session, which stops the Recall meter (leave_call) —
    # stub it like every other finalize test does, or the key-free suite dies
    # on assert_ready (and a keyed machine would hit the real Recall API).
    monkeypatch.setattr(recall_client, "leave_call", lambda bot_id: None)
    try:
        resp = client.post("/sessions/bot_unowned/end")
        assert resp.status_code in (200, 202)
    finally:
        if store.get("bot_unowned"):
            store.remove("bot_unowned")


def test_email_allowlist_blocks_outsider(client, google_on, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_allowed_emails", "@trusted.com, ceo@acme.io")
    assert auth.email_allowed("anyone@trusted.com") is True
    assert auth.email_allowed("ceo@acme.io") is True
    assert auth.email_allowed("stranger@gmail.com") is False


def test_empty_allowlist_allows_all():
    assert auth.email_allowed("whoever@wherever.com") is True


def test_allowed_endpoint_gates_by_email(client, monkeypatch):
    # No allow-list configured -> not gated, everyone passes (login page shows
    # Google directly).
    r = client.get("/auth/allowed").json()
    assert r["gated"] is False and r["allowed"] is True

    # Allow-list on -> gated; only listed emails / domains may proceed.
    monkeypatch.setattr(settings, "dashboard_allowed_emails", "vip@acme.com, @trusted.com")
    assert client.get("/auth/allowed").json()["gated"] is True
    assert client.get("/auth/allowed", params={"email": "vip@acme.com"}).json()["allowed"] is True
    assert client.get("/auth/allowed", params={"email": "me@trusted.com"}).json()["allowed"] is True
    assert client.get("/auth/allowed", params={"email": "stranger@gmail.com"}).json()["allowed"] is False
    # never leaks the list itself
    assert "acme" not in str(client.get("/auth/allowed").json())


def test_non_ascii_cookie_does_not_crash():
    """A hostile cookie/state with a non-ASCII byte must not raise (hmac.
    compare_digest on a non-ASCII str raises TypeError) — it must read as
    invalid so the caller falls to the login gate, never a 500. Exercised at
    the unit boundary: httpx's test transport refuses to even send a non-ASCII
    header, so a live request can't reproduce the server-side path here."""
    assert auth.read_cookie("abc.\xe9\xff") is None
    assert auth._verify("abc", "\xe9", "session") is False
    assert auth.read_cookie("\xe9.\xff") is None


def test_stats_scoped_to_org(client, google_on):
    alice = _login(client, "alice@example.com")
    store.save_artifact("b1", _artifact(alice["org_id"], "mine one"))
    store.save_artifact("b2", _artifact(alice["org_id"], "mine two"))
    bob = store.upsert_user(email="bob@example.com")
    store.save_artifact("b3", _artifact(bob["org_id"], "bob's"))
    stats = client.get("/dashboard/summary").json()["stats"]
    assert stats["meetings_30d"] == 2  # bob's row excluded from the rollup too
