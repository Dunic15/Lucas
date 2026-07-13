"""Cedric production completion, Laura side (PR D) — key-free.

Proves the four self-serve seams on SQLite with zero external services:

1. PER-ORG machine bearers are first-class on every Cedric-facing endpoint:
   a bearer that resolves via org_tokens acts ONLY on its own org's rows
   (own → ok, another org's → 403); the GLOBAL laura_api_token keeps its
   Demo service scope; key-free stays open.
2. /dashboard/connections/brain/slack/complete binds credentials only through
   signed state + the pending nonce. Same-state retries return the same hashed-
   only org_token; a new nonce rotates it and stale states cannot roll it back.
3. The brain-connectors catalog is scoped to the CALLER's org (upstream query
   carries their team_id; a fresh org reads not_connected, never the demo
   team's catalog), and disconnect revokes REMOTELY FIRST — local state and
   the signing secret survive a failed revoke untouched.
4. A logged-in fresh org reads calendar/gmail/drive/slack as NOT connected
   even when the platform's global env is configured; the anonymous demo and
   the global bearer keep today's global flags.

The Postgres control-plane half (real RLS) lives in test_control_plane_pg.py;
the durable mirror seams here are proven with monkeypatched control_plane
functions (same module object dashboard.py lazily imports).
"""
from __future__ import annotations

import hashlib
import importlib
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, cedric, control_plane, ledger, store
from app.cedric import callback, install_state, secret_registry
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file — needs its tables too
    return TestClient(main_module.app)


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setattr(settings, "session_secret", "test-secret")
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid-test")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csecret-test")


def _login(client, email="owner@example.com", name="Owner") -> dict:
    user = store.upsert_user(email=email, name=name)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def _bearer(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def _request_with(bearer: str | None):
    headers = {} if bearer is None else {"authorization": f"Bearer {bearer}"}
    return types.SimpleNamespace(headers=headers)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers: dict = {}

    def json(self):
        return self._payload


# ── 1. resolve_machine_org + the endpoint sweep ────────────────────────

def test_resolve_machine_org_contract(client, monkeypatch):
    """global token → demo org; per-org token → its org; junk/absent → None.
    The raw token is only ever hashed/compared (org_tokens stores sha256)."""
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    raw = store.mint_org_token("org_sff", "svc")
    assert cedric.resolve_machine_org(_request_with("sesame")) == settings.demo_org_id
    assert cedric.resolve_machine_org(_request_with(raw)) == "org_sff"
    assert cedric.resolve_machine_org(_request_with("junk")) is None
    assert cedric.resolve_machine_org(_request_with(None)) is None
    # global token unset: the same raw global string is just an unknown token
    monkeypatch.setattr(settings, "laura_api_token", "")
    assert cedric.resolve_machine_org(_request_with("sesame")) is None
    assert cedric.resolve_machine_org(_request_with(raw)) == "org_sff"



def test_durable_token_miss_never_falls_back_to_sqlite(client, monkeypatch):
    """A token revoked in Postgres cannot survive in the local cache."""
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(control_plane, "resolve_org_token", lambda raw: None)
    local_calls: list[str] = []
    monkeypatch.setattr(
        store,
        "resolve_org_token",
        lambda raw: local_calls.append(raw) or "org_stale",
    )
    request = _request_with("stale-token")
    assert cedric.resolve_machine_org(request) is None
    assert main_module._org_token_bearer_org(request) is None
    assert local_calls == []


def test_customer_callback_urls_are_bound_to_cedric_https_origin(monkeypatch):
    monkeypatch.setattr(
        settings, "cedric_orgs_url", "https://www.meet-cedric.com/api/laura/orgs"
    )
    safe = types.SimpleNamespace(
        callback_url="https://www.meet-cedric.com/api/laura/events",
        context_url="https://www.meet-cedric.com/api/laura/context",
    )
    hostile = types.SimpleNamespace(
        callback_url="https://attacker.example/steal", context_url=""
    )
    insecure = types.SimpleNamespace(
        callback_url="http://www.meet-cedric.com/api/laura/events", context_url=""
    )
    assert cedric.request_integration_urls_allowed(safe, "org_customer") is True
    assert cedric.request_integration_urls_allowed(hostile, "org_customer") is False
    assert cedric.request_integration_urls_allowed(insecure, "org_customer") is False
    for blocked in (
        "https://127.0.0.1/callback",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.8/callback",
        "https://user:pass@www.meet-cedric.com/callback",
        "https://www.meet-cedric.com:8443/callback",
    ):
        req = types.SimpleNamespace(callback_url=blocked, context_url="")
        assert cedric.request_integration_urls_allowed(req, "org_customer") is False
    # Demo/key-free wiring is unchanged.
    assert cedric.request_integration_urls_allowed(hostile, settings.demo_org_id) is True



def test_cookie_user_cannot_supply_integration_wiring(
    client, monkeypatch, google_on
):
    """The browser chooses meeting/avatar only; org routing is server-owned."""
    from app import recall_client

    _login(client)
    monkeypatch.setattr(recall_client, "assert_ready", lambda: None)
    created: list[tuple] = []
    monkeypatch.setattr(
        recall_client, "create_bot", lambda *a, **k: created.append((a, k))
    )
    response = client.post(
        "/sessions/start",
        json={
            "meeting_url": "https://meet.google.com/no-browser-routing",
            "avatar_id": "cedric",
            "callback_url": "https://www.meet-cedric.com/api/laura/events",
            "external_ref": {"team": "T1"},
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "integration wiring is managed by your workspace"
    assert created == []


def test_org_token_scopes_cancel(client, monkeypatch):
    """A per-org bearer cancels ONLY its own org's sessions; another org's
    stays running and answers a 404 BYTE-IDENTICAL to a nonexistent bot_id —
    no cross-tenant existence oracle (adversarial review should-fix 2). The
    global bearer is scoped to the Demo org."""
    from app import recall_client

    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    monkeypatch.setattr(recall_client, "leave_call", lambda bot_id: None)
    raw = store.mint_org_token("org_sff", "svc")

    store.create("bot_c_own", "https://meet.google.com/c1", "laura", org_id="org_sff")
    store.create("bot_c_other", "https://meet.google.com/c2", "laura", org_id="org_other")
    try:
        denied = client.post("/sessions/bot_c_other/cancel", headers=_bearer(raw))
        ghost = client.post("/sessions/bot_c_ghost/cancel", headers=_bearer(raw))
        assert denied.status_code == ghost.status_code == 404
        assert denied.content == ghost.content  # indistinguishable
        assert store.get("bot_c_other") is not None  # left running

        ok = client.post("/sessions/bot_c_own/cancel", headers=_bearer(raw))
        assert ok.status_code == 200 and ok.json()["cancelled"] is True
        assert store.get("bot_c_own") is None

        # The deployment bearer is Demo-only, never a tenant master key.
        legacy = client.post("/sessions/bot_c_other/cancel", headers=_bearer("sesame"))
        assert legacy.status_code == 404
        assert store.get("bot_c_other") is not None
    finally:
        for bot in ("bot_c_own", "bot_c_other"):
            if store.get(bot):
                store.remove(bot)


def test_org_token_scopes_artifact_read(client, monkeypatch):
    """Wrong-org bot_ids — finalized OR live — answer the IDENTICAL 404 as a
    nonexistent one: /artifact is never an existence/progress oracle."""
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    raw = store.mint_org_token("org_sff", "svc")
    store.save_artifact("b_art_own", {"summary": "s", "org_id": "org_sff"})
    store.save_artifact("b_art_other", {"summary": "s", "org_id": "org_other"})

    ok = client.get("/sessions/b_art_own/artifact", headers=_bearer(raw))
    assert ok.status_code == 200 and ok.json()["status"] == "done"
    ghost = client.get("/sessions/b_art_ghost/artifact", headers=_bearer(raw))
    denied = client.get("/sessions/b_art_other/artifact", headers=_bearer(raw))
    assert denied.status_code == ghost.status_code == 404
    assert denied.content == ghost.content  # indistinguishable
    # a LIVE session of another org: same identical 404 — no in_progress probe
    store.create("bot_live_x", "https://meet.google.com/ax", "laura", org_id="org_other")
    try:
        live = client.get("/sessions/bot_live_x/artifact", headers=_bearer(raw))
        assert live.status_code == 404 and live.content == ghost.content
    finally:
        store.remove("bot_live_x")
    # The global service bearer is Demo-scoped, not cross-tenant.
    legacy = client.get("/sessions/b_art_other/artifact", headers=_bearer("sesame"))
    assert legacy.status_code == 404


def test_org_token_scopes_deliver(client, monkeypatch):
    from app import actions

    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    monkeypatch.setattr(actions, "send_email", lambda *a, **k: {"sent": False})
    monkeypatch.setattr(actions, "post_to_slack", lambda *a, **k: {"sent": False})
    raw = store.mint_org_token("org_sff", "svc")
    store.save_artifact("b_dl_own", {"summary": "s", "org_id": "org_sff"})
    store.save_artifact("b_dl_other", {"summary": "s", "org_id": "org_other"})

    ok = client.post(
        "/sessions/b_dl_own/deliver", headers=_bearer(raw), json={"to": [], "slack": False}
    )
    assert ok.status_code == 200
    denied = client.post(
        "/sessions/b_dl_other/deliver", headers=_bearer(raw), json={"to": [], "slack": False}
    )
    ghost = client.post(
        "/sessions/b_dl_ghost/deliver", headers=_bearer(raw), json={"to": [], "slack": False}
    )
    # wrong-org == not-found, byte-identical (should-fix 2)
    assert denied.status_code == ghost.status_code == 404
    assert denied.content == ghost.content


def test_org_token_scopes_ledger_and_org_memory(client, monkeypatch):
    """/ledger + /org/* read the BEARER org's memory: a per-org token sees its
    rows, the global bearer keeps the Demo org, key-free open keeps Demo."""
    url = "https://meet.google.com/led-scope"
    ledger.record_meeting(
        url, "laura", "b1",
        {"actions": [{"owner": "A", "item": "own-org task"}]},
        org_id="org_sff",
    )
    ledger.record_meeting(
        url, "laura", "b2",
        {"actions": [{"owner": "B", "item": "demo-org task"}]},
    )  # default org: demo
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    raw = store.mint_org_token("org_sff", "svc")

    mine = client.get("/ledger", params={"meeting_url": url}, headers=_bearer(raw))
    assert mine.status_code == 200
    assert [i["item"] for i in mine.json()["items"]] == ["own-org task"]
    legacy = client.get("/ledger", params={"meeting_url": url}, headers=_bearer("sesame"))
    assert [i["item"] for i in legacy.json()["items"]] == ["demo-org task"]

    mine = client.get("/org/actions", headers=_bearer(raw)).json()["open"]
    assert [i["item"] for g in mine.values() for i in g] == ["own-org task"]
    legacy = client.get("/org/actions", headers=_bearer("sesame")).json()["open"]
    assert [i["item"] for g in legacy.values() for i in g] == ["demo-org task"]

    mine = client.get("/org/search", params={"q": "task"}, headers=_bearer(raw)).json()
    assert [i["item"] for i in mine["ledger_matches"]] == ["own-org task"]

    # resolve: a per-org bearer cannot close another org's row
    demo_item = ledger.items(ledger.meeting_key(url), org_id=settings.demo_org_id)[0]
    denied = client.post(f"/org/actions/{demo_item['id']}/resolve", headers=_bearer(raw))
    assert denied.status_code == 404  # scoped update matched nothing
    assert ledger.items(
        ledger.meeting_key(url), status="open", org_id=settings.demo_org_id
    )  # still open


def test_org_action_status_is_tenant_scoped_before_finalize(client, monkeypatch):
    """The same action_id may progress independently in two organizations.

    A per-org bearer may report pre-finalize state because the status row is
    keyed by (org_id, action_id). Finalization consults only its own org's row.
    """
    url = "https://meet.google.com/status-scope"
    same_id = "aid00000000000a"
    ledger.record_meeting(
        url, "laura", "b_own",
        {"actions": [{"owner": "A", "item": "send the doc", "action_id": same_id}]},
        org_id="org_sff",
    )
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    own = store.mint_org_token("org_sff", "svc")
    other = store.mint_org_token("org_other", "svc")

    # Org B can report the same id before its meeting finalizes; this must not
    # close or decorate org A's row.
    pre = client.post(
        f"/org/actions/{same_id}/status",
        headers=_bearer(other),
        json={"status": "done", "detail": "org B finished"},
    )
    assert pre.status_code == 200
    assert ledger.items(ledger.meeting_key(url), status="open", org_id="org_sff")
    assert ledger.action_statuses([same_id], org_id="org_sff") == {}
    assert ledger.action_statuses([same_id], org_id="org_other")[same_id][
        "status"
    ] == "done"

    # Org A's status is independent and closes only org A's ledger row.
    ok = client.post(
        f"/org/actions/{same_id}/status",
        headers=_bearer(own),
        json={"status": "done", "detail": "org A executed"},
    )
    assert ok.status_code == 200
    assert not ledger.items(ledger.meeting_key(url), status="open", org_id="org_sff")
    assert ledger.action_statuses([same_id], org_id="org_sff")[same_id][
        "detail"
    ] == "org A executed"

    # When org B later finalizes, its pre-finalize terminal status is preserved.
    other_url = "https://meet.google.com/status-scope-b"
    ledger.record_meeting(
        other_url, "laura", "b_other",
        {"actions": [{"owner": "B", "item": "send the other doc", "action_id": same_id}]},
        org_id="org_other",
    )
    assert not ledger.items(
        ledger.meeting_key(other_url), status="open", org_id="org_other"
    )

    # The global service bearer is Demo-scoped, not a cross-tenant writer.
    demo_id = "aid_demo_prefinal0"
    svc = client.post(
        f"/org/actions/{demo_id}/status",
        headers=_bearer("sesame"),
        json={"status": "proposed"},
    )
    assert svc.status_code == 200
    assert ledger.action_statuses([demo_id], org_id=settings.demo_org_id)[demo_id][
        "status"
    ] == "proposed"
    assert ledger.action_statuses([demo_id], org_id="org_other") == {}

def test_summary_scoped_for_per_org_bearer(client, monkeypatch):
    """/dashboard/summary's machine path: a per-org bearer sees its org + the
    legacy unowned rows — never the Demo org's; the global bearer sees only
    Demo + legacy rows."""
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    raw = store.mint_org_token("org_sff", "svc")
    store.save_artifact("b_sum_own", {"summary": "own row", "org_id": "org_sff"})
    store.save_artifact(
        "b_sum_demo",
        {"summary": "demo row", "org_id": settings.demo_org_id},
        org_id=settings.demo_org_id,
    )
    store.save_artifact("b_sum_legacy", {"summary": "legacy row", "org_id": ""})
    store.set_connection("org_sff", "cedric", "cedric-brain", "connected", {"team_id": "T1"})

    scoped = client.get("/dashboard/summary", headers=_bearer(raw)).json()
    assert {m["summary"] for m in scoped["meetings"]} == {"own row", "legacy row"}
    assert [c["provider"] for c in scoped["org_connections"]] == ["cedric-brain"]

    legacy = client.get("/dashboard/summary", headers=_bearer("sesame")).json()
    assert {m["summary"] for m in legacy["meetings"]} == {
        "demo row", "legacy row"
    }


# ── 2. slack/complete binds ONLY an initiated install ──────────────────

def test_slack_complete_rejects_uninitiated_org(client, monkeypatch):
    """No signed state: the machine bearer alone cannot bind credentials."""
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = store.upsert_user("victim@example.com", "Victim")
    writes: list = []
    monkeypatch.setattr(
        secret_registry, "upsert_org_credentials", lambda *a: writes.append(a) or True
    )

    resp = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json={
            "org_id": user["org_id"],
            "avatar_id": "cedric",
            "team_id": "T_EVIL",
            "webhook_secret": "attacker-secret",
            "webhook_token": "attacker-token",
        },
    )
    assert resp.status_code == 400
    assert writes == []
    assert store.connections_for_org(user["org_id"]) == []


def test_slack_start_complete_full_roundtrip(client, monkeypatch, google_on):
    """Full flow plus lost-response retry: the same server completion returns
    the same org token without persisting its raw value."""
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(settings, "cedric_orgs_token", "shared-test-token")
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = _login(client)
    writes: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        secret_registry,
        "upsert_org_credentials",
        lambda org, secret, token: writes.append((org, secret, token)) or True,
    )

    start = client.get(
        "/dashboard/connections/brain/slack/start",
        params={"avatar_id": "cedric", "channel": "#approvals"},
        follow_redirects=False,
    )
    assert start.status_code == 302
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    rows = store.connections_for_org(user["org_id"])
    assert rows and rows[0]["provider"] == "cedric-brain"
    assert rows[0]["status"] == "pending"  # the initiation record
    state_nonce = install_state.unpack(state)["nonce"]
    assert rows[0]["config"]["pending_install_nonce"] == state_nonce

    resp = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json={
            "org_id": user["org_id"],
            "avatar_id": "cedric",
            "team_id": "T_RT",
            "channel": "#approvals",
            "webhook_secret": "minted-by-cedric",
            "webhook_token": "cedric-workspace-token",
            "state": state,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "connected"
    assert writes == [
        (user["org_id"], "minted-by-cedric", "cedric-workspace-token")
    ]
    row = store.connections_for_org(user["org_id"])[0]
    assert row["status"] == "connected"
    assert row["config"]["team_id"] == "T_RT"
    assert row["config"]["channel"] == "#approvals"
    assert row["config"]["install_nonce"]
    token = body["org_token"]
    assert token and store.resolve_org_token(token) == user["org_id"]
    with store._connect() as conn:
        hashes = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM org_tokens")]
    assert token not in hashes

    # Simulate Cedric losing the first HTTP 200 after Laura committed.
    replay = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json={
            "org_id": user["org_id"],
            "avatar_id": "cedric",
            "team_id": "T_RT",
            "channel": "#approvals",
            "webhook_secret": "minted-by-cedric",
            "webhook_token": "cedric-workspace-token",
            "state": state,
        },
    )
    assert replay.status_code == 200
    assert replay.json()["org_token"] == token
    assert replay.json()["idempotent_replay"] is True



def test_same_state_replay_is_bound_to_original_workspace_envelope(
    client, monkeypatch
):
    """A stolen/lost-response state cannot be replayed with different workspace
    routing or credentials, and mismatches never reach the secret registry."""
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = store.upsert_user("bound-replay@example.com")
    state = install_state.pack(user["org_id"], "cedric", "", "")
    nonce = install_state.unpack(state)["nonce"]
    assert store.begin_brain_install(user["org_id"], "cedric", nonce)

    writes: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        secret_registry,
        "upsert_org_credentials",
        lambda org, secret, token: writes.append((org, secret, token)) or True,
    )
    original = {
        "org_id": user["org_id"],
        "avatar_id": "cedric",
        "team_id": "T_BOUND",
        "channel": "",
        "webhook_secret": "bound-secret",
        "webhook_token": "bound-peer",
        "state": state,
    }
    first = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json=original,
    )
    assert first.status_code == 200
    row = store.connections_for_org(user["org_id"])[0]
    assert row["config"]["webhook_secret_sha256"] == hashlib.sha256(
        b"bound-secret"
    ).hexdigest()
    assert row["config"]["webhook_token_sha256"] == hashlib.sha256(
        b"bound-peer"
    ).hexdigest()
    assert "webhook_secret" not in row["config"]
    assert "webhook_token" not in row["config"]

    writes.clear()
    mutations = (
        {"team_id": "T_OTHER"},
        {"channel": "#other"},
        {"webhook_secret": "other-secret"},
        {"webhook_token": "other-peer"},
    )
    for mutation in mutations:
        changed = {**original, **mutation}
        replay = client.post(
            "/dashboard/connections/brain/slack/complete",
            headers=_bearer("provisioning-token"),
            json=changed,
        )
        assert replay.status_code == 409
        assert replay.json() == {"error": "install replay does not match"}
    assert writes == []



def test_reinstall_rotates_token_same_state_retries_and_old_state_is_stale(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = store.upsert_user("rotate@example.com")
    monkeypatch.setattr(secret_registry, "upsert_org_credentials", lambda *a: True)

    def complete(state: str):
        return client.post(
            "/dashboard/connections/brain/slack/complete",
            headers=_bearer("provisioning-token"),
            json={
                "org_id": user["org_id"],
                "avatar_id": "cedric",
                "team_id": "T_ROT",
                "channel": "",
                "webhook_secret": "secret",
                "webhook_token": "peer",
                "state": state,
            },
        )

    first_state = install_state.pack(user["org_id"], "cedric", "", "")
    first_nonce = install_state.unpack(first_state)["nonce"]
    assert store.begin_brain_install(user["org_id"], "cedric", first_nonce)
    first = complete(first_state)
    assert first.status_code == 200
    first_token = first.json()["org_token"]

    second_state = install_state.pack(user["org_id"], "cedric", "", "")
    second_nonce = install_state.unpack(second_state)["nonce"]
    assert store.begin_brain_install(user["org_id"], "cedric", second_nonce)

    # P0 regression: once a newer start is pending, an old installed nonce is
    # stale — it must not return 200 or resync old workspace credentials.
    old_during_new = complete(first_state)
    assert old_during_new.status_code == 409
    assert old_during_new.json()["error"] == "stale install state"
    assert store.resolve_org_token(first_token) == user["org_id"]
    pending = store.connections_for_org(user["org_id"])[0]["config"]
    assert pending["pending_install_nonce"] == second_nonce

    second = complete(second_state)
    assert second.status_code == 200
    second_token = second.json()["org_token"]
    assert second_token != first_token
    assert store.resolve_org_token(first_token) is None
    assert store.resolve_org_token(second_token) == user["org_id"]

    replay = complete(second_state)
    assert replay.status_code == 200
    assert replay.json()["org_token"] == second_token
    assert replay.json()["idempotent_replay"] is True

    # A delayed callback from the older OAuth tab cannot roll credentials back.
    stale = complete(first_state)
    assert stale.status_code == 409
    assert stale.json()["error"] == "stale install state"
    assert store.resolve_org_token(second_token) == user["org_id"]
    assert store.resolve_org_token(first_token) is None



def test_concurrent_same_state_sqlite_guard_converges(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = store.upsert_user("concurrent-install@example.com")
    state = install_state.pack(user["org_id"], "cedric", "", "")
    nonce = install_state.unpack(state)["nonce"]
    raw = install_state.derive_org_token(user["org_id"], nonce)
    assert store.begin_brain_install(user["org_id"], "cedric", nonce)

    def accept(_):
        return store.accept_brain_install(
            user["org_id"], "cedric", nonce, raw, "T_CONCURRENT", "",
            "concurrent-secret", "concurrent-peer",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(accept, range(8)))

    assert outcomes.count("applied") == 1
    assert outcomes.count("replay") == 7
    assert store.resolve_org_token(raw) == user["org_id"]
    with store._connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM org_tokens "
            "WHERE org_id = ? AND label = 'cedric-slack-install'",
            (user["org_id"],),
        ).fetchone()[0]
    assert count == 1


def test_org_token_derivation_survives_process_restart(monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = store.upsert_user("restart-install@example.com")
    state = install_state.pack(user["org_id"], "cedric", "", "")
    nonce = install_state.unpack(state)["nonce"]
    before = install_state.derive_org_token(user["org_id"], nonce)
    assert store.begin_brain_install(user["org_id"], "cedric", nonce)
    assert (
        store.accept_brain_install(
            user["org_id"], "cedric", nonce, before, "T_RESTART", "",
            "restart-secret", "restart-peer",
        )
        == "applied"
    )

    # The raw value is not cached or stored. A fresh module instance derives
    # the exact same response from the signed nonce after a process restart.
    reloaded = importlib.reload(install_state)
    after = reloaded.derive_org_token(user["org_id"], nonce)
    assert after == before
    assert (
        store.accept_brain_install(
            user["org_id"], "cedric", nonce, after, "T_RESTART", "",
            "restart-secret", "restart-peer",
        )
        == "replay"
    )
    assert store.resolve_org_token(after) == user["org_id"]



def test_slack_complete_state_alone_proves_initiation(client, monkeypatch):
    """A valid signed state binds the install even if the pending row was lost
    (redeploy wiped the ephemeral store between start and complete)."""
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = store.upsert_user("stateful@example.com")
    monkeypatch.setattr(secret_registry, "upsert_org_credentials", lambda *a: True)
    state = install_state.pack(user["org_id"], "cedric", "#ops", "https://x/dash")

    resp = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json={
            "org_id": user["org_id"],
            "avatar_id": "cedric",
            "team_id": "T_ST",
            "channel": "#ops",
            "webhook_secret": "minted",
            "webhook_token": "cedric-workspace-token",
            "state": state,
        },
    )
    assert resp.status_code == 200 and resp.json()["status"] == "connected"


def test_slack_complete_state_org_mismatch_hard_fails(client, monkeypatch):
    """A state minted for org A spliced onto org B's complete is refused even
    when B has a pending row — a mismatched signed state is an attack signal."""
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    alice = store.upsert_user("alice-state@example.com")
    bob = store.upsert_user("bob-state@example.com")
    store.set_connection(bob["org_id"], "cedric", "cedric-brain", "pending", {})
    writes: list = []
    monkeypatch.setattr(
        secret_registry, "upsert_org_credentials", lambda *a: writes.append(a) or True
    )
    alice_state = install_state.pack(alice["org_id"], "cedric", "", "")

    resp = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json={
            "org_id": bob["org_id"],
            "avatar_id": "cedric",
            "team_id": "T_X",
            "webhook_secret": "minted",
            "webhook_token": "cedric-workspace-token",
            "state": alice_state,
        },
    )
    assert resp.status_code == 403
    assert writes == []


def test_slack_complete_per_org_bearer_own_org_only(client, monkeypatch):
    """A per-org bearer may complete installs ONLY for its own org."""
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    alice = store.upsert_user("alice-tok@example.com")
    bob = store.upsert_user("bob-tok@example.com")
    for u in (alice, bob):
        store.set_connection(u["org_id"], "cedric", "cedric-brain", "pending", {})
    monkeypatch.setattr(secret_registry, "upsert_org_credentials", lambda *a: True)
    alice_token = store.mint_org_token(alice["org_id"], "ws")
    alice_state = install_state.pack(alice["org_id"], "cedric", "", "")
    bob_state = install_state.pack(bob["org_id"], "cedric", "", "")

    denied = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer(alice_token),
        json={
            "org_id": bob["org_id"], "avatar_id": "cedric",
            "team_id": "T_B", "webhook_secret": "s",
            "webhook_token": "peer-b", "state": bob_state,
        },
    )
    assert denied.status_code == 403
    ok = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer(alice_token),
        json={
            "org_id": alice["org_id"], "avatar_id": "cedric",
            "team_id": "T_A", "webhook_secret": "s",
            "webhook_token": "peer-a", "state": alice_state,
        },
    )
    assert ok.status_code == 200


def test_slack_complete_mirrors_durable_control_plane(client, monkeypatch):
    """The endpoint delegates one indivisible PG+SSM completion saga."""
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = store.upsert_user("durable@example.com")
    store.set_connection(user["org_id"], "cedric", "cedric-brain", "pending", {})
    completed: list[tuple] = []
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(control_plane, "get_connections", lambda org: [])
    monkeypatch.setattr(
        control_plane,
        "complete_brain_install",
        lambda *a: completed.append(a) or "applied",
    )
    state = install_state.pack(user["org_id"], "cedric", "", "")
    nonce = install_state.unpack(state)["nonce"]
    response = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json={
            "org_id": user["org_id"], "avatar_id": "cedric",
            "team_id": "T_D", "webhook_secret": "s",
            "webhook_token": "peer-d", "state": state,
        },
    )
    assert response.status_code == 200
    expected = install_state.derive_org_token(user["org_id"], nonce)
    assert response.json()["org_token"] == expected
    assert completed == [
        (
            user["org_id"], "cedric", nonce, expected, "T_D", "",
            "s", "peer-d",
        )
    ]
    # A post-lock warm-cache write could race a disconnect. Durable state is
    # authoritative, so the pre-existing local initiation row stays pending.
    assert store.connections_for_org(user["org_id"])[0]["status"] == "pending"


def test_durable_replay_stays_pending_until_secret_sync_succeeds(
    client, monkeypatch
):
    """A registry failure is retryable and never advertises connected."""
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = store.upsert_user("durable-replay-pending@example.com")
    store.set_connection(user["org_id"], "cedric", "cedric-brain", "pending", {})
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(control_plane, "get_connections", lambda org: [])
    seen: list[str] = []

    def fail_complete(*_args):
        seen.append(store.connections_for_org(user["org_id"])[0]["status"])
        return "registry_failed"

    monkeypatch.setattr(control_plane, "complete_brain_install", fail_complete)
    state = install_state.pack(user["org_id"], "cedric", "", "")
    response = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json={
            "org_id": user["org_id"], "avatar_id": "cedric",
            "team_id": "T_D", "webhook_secret": "s",
            "webhook_token": "peer-d", "state": state,
        },
    )
    assert response.status_code == 503
    assert response.json() == {"error": "registry update failed"}
    assert seen == ["pending"]
    assert store.connections_for_org(user["org_id"])[0]["status"] == "pending"


def test_install_state_unpack_contract(monkeypatch):
    monkeypatch.setattr(settings, "cedric_orgs_token", "sign-key")
    state = install_state.pack(
        "org_a", "cedric", "#ch", "https://x/d", "https://x/complete", now=1000.0
    )
    data = install_state.unpack(state, now=1200.0)
    assert data and data["org_id"] == "org_a" and data["avatar_id"] == "cedric"
    assert data["complete_url"] == "https://x/complete"
    assert install_state.unpack(state, now=1000.0 + 601) is None  # expired
    tampered = state[:-1] + ("0" if state[-1] != "0" else "1")
    assert install_state.unpack(tampered, now=1200.0) is None  # tampered
    assert install_state.unpack("garbage", now=1200.0) is None
    monkeypatch.setattr(settings, "cedric_orgs_token", "other-key")
    assert install_state.unpack(state, now=1200.0) is None  # wrong key


# ── 3. connectors catalog scoping + remote-revoke-first disconnect ─────

def test_connectors_upstream_query_carries_callers_team(client, monkeypatch, google_on):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    user = _login(client)
    store.set_connection(
        user["org_id"], "cedric", "cedric-brain", "connected",
        {"team_id": "T_MINE", "channel": "#ops"},
    )
    calls: list[tuple[str, dict, str | None]] = []
    monkeypatch.setattr(
        secret_registry, "bearer_for", lambda org: "workspace-token"
    )

    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            calls.append(
                (url, dict(params or {}), (headers or {}).get("Authorization"))
            )
            return _FakeResponse(200, {"org_id": user["org_id"], "connectors": []})

    monkeypatch.setattr(callback.httpx, "Client", FakeClient)
    resp = client.get("/dashboard/connections/brain/connectors")
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    assert calls == [
        (
            "https://cedric/api/laura/connectors",
            {"org_id": user["org_id"], "team": "T_MINE"},
            "Bearer workspace-token",
        )
    ]


def test_disconnect_fences_org_token_before_remote_cleanup_and_retries(
    client, monkeypatch, google_on
):
    """Once disconnect starts, an old Cedric bearer cannot dispatch even while
    remote cleanup is blocked/failing. Retry stays fail-closed and converges."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app import recall_client

    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(settings, "laura_api_token", "deployment-token")
    user = _login(client)
    other = store.upsert_user("disconnect-other@example.com")
    store.set_connection(
        user["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T1"}
    )
    raw = store.rotate_org_token(user["org_id"], "cedric-slack-install")
    other_raw = store.rotate_org_token(other["org_id"], "cedric-slack-install")
    assert store.resolve_org_token(raw) == user["org_id"]
    assert store.resolve_org_token(other_raw) == other["org_id"]

    created: list[str] = []
    monkeypatch.setattr(recall_client, "assert_ready", lambda: None)

    def create_bot(*args, **kwargs):
        bot_id = f"bot-disconnect-auth-{len(created) + 1}"
        created.append(bot_id)
        return {"id": bot_id}

    monkeypatch.setattr(recall_client, "create_bot", create_bot)
    machine = TestClient(main_module.app)
    before = machine.post(
        "/sessions/start",
        headers=_bearer(raw),
        json={"meeting_url": "https://meet.google.com/disconnect-before"},
    )
    assert before.status_code == 200, before.text
    store.remove(created[-1])

    remote_entered = Event()
    release_remote = Event()
    remote_calls: list[str] = []

    def blocked_failure(org):
        remote_calls.append(org)
        remote_entered.set()
        assert release_remote.wait(10)
        return 500

    monkeypatch.setattr(cedric, "revoke_org", blocked_failure)
    removed: list[str] = []
    monkeypatch.setattr(
        secret_registry,
        "remove_org_credentials",
        lambda org: removed.append(org) or True,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.post,
            "/dashboard/connections/brain/disconnect",
            json={"avatar_id": "cedric"},
        )
        assert remote_entered.wait(10)
        # The token revoke and disconnecting fence committed before the remote
        # call began. A machine request cannot use the logged-in browser cookie.
        assert store.resolve_org_token(raw) is None
        denied = machine.post(
            "/sessions/start",
            headers=_bearer(raw),
            json={"meeting_url": "https://meet.google.com/disconnect-blocked"},
        )
        assert denied.status_code == 401
        other_ok = machine.post(
            "/sessions/start",
            headers=_bearer(other_raw),
            json={"meeting_url": "https://meet.google.com/disconnect-other"},
        )
        assert other_ok.status_code == 200, other_ok.text
        store.remove(created[-1])
        release_remote.set()
        failed = future.result(timeout=10)

    assert failed.status_code == 502
    assert failed.json() == {"error": "remote_revoke_failed"}
    assert removed == []
    checkpoint = store.connections_for_org(user["org_id"])[0]
    assert checkpoint["status"] == "disconnecting"
    assert checkpoint["config"]["disconnect_phase"] == "revoke_pending"
    assert store.resolve_org_token(raw) is None
    assert store.resolve_org_token(other_raw) == other["org_id"]

    monkeypatch.setattr(
        cedric, "revoke_org", lambda org: remote_calls.append(org) or 204
    )
    retry = client.post(
        "/dashboard/connections/brain/disconnect",
        json={"avatar_id": "cedric"},
    )
    assert retry.status_code == 200
    assert remote_calls == [user["org_id"], user["org_id"]]
    assert removed == [user["org_id"]]
    final = store.connections_for_org(user["org_id"])[0]
    assert final["status"] == "disconnected"
    assert final["config"]["install_tombstone"] is True
    assert store.resolve_org_token(raw) is None
    assert store.resolve_org_token(other_raw) == other["org_id"]


def test_disconnect_remote_revoke_ok_drops_secret(def test_disconnect_remote_revoke_ok_drops_secret(client, monkeypatch, google_on):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    user = _login(client)
    store.set_connection(
        user["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T1"}
    )
    removed: list = []
    monkeypatch.setattr(
        secret_registry, "remove_org_credentials", lambda org: removed.append(org) or True
    )
    deleted: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        secret_registry, "bearer_for", lambda org: "workspace-token"
    )

    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def delete(self, url, headers=None):
            deleted.append((url, (headers or {}).get("Authorization")))
            return _FakeResponse(204)

    monkeypatch.setattr(callback.httpx, "Client", FakeClient)
    resp = client.post(
        "/dashboard/connections/brain/disconnect", json={"avatar_id": "cedric"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disconnected"
    assert resp.json()["remote_revoked"] is True
    assert deleted == [
        (
            f"https://cedric/api/laura/orgs/{user['org_id']}",
            "Bearer workspace-token",
        )
    ]
    assert removed == [user["org_id"]]
    row = store.connections_for_org(user["org_id"])[0]
    assert row["status"] == "disconnected"


def test_disconnect_remote_404_counts_as_revoked(client, monkeypatch, google_on):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    user = _login(client)
    store.set_connection(
        user["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T1"}
    )
    monkeypatch.setattr(secret_registry, "remove_org_credentials", lambda org: True)

    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def delete(self, url, headers=None):
            return _FakeResponse(404)

    monkeypatch.setattr(callback.httpx, "Client", FakeClient)
    resp = client.post(
        "/dashboard/connections/brain/disconnect", json={"avatar_id": "cedric"}
    )
    assert resp.status_code == 200 and resp.json()["status"] == "disconnected"


def test_disconnect_without_orchestrator_is_local_only(client, monkeypatch, google_on):
    """CEDRIC_ORGS_URL unset (local/key-free): nothing remote exists — the
    local disconnect proceeds and reports remote_revoked False."""
    monkeypatch.setattr(settings, "cedric_orgs_url", "")
    user = _login(client)
    store.set_connection(
        user["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T1"}
    )
    monkeypatch.setattr(secret_registry, "remove_org_credentials", lambda org: True)
    resp = client.post(
        "/dashboard/connections/brain/disconnect", json={"avatar_id": "cedric"}
    )
    assert resp.status_code == 200
    assert resp.json()["remote_revoked"] is False
    assert store.connections_for_org(user["org_id"])[0]["status"] == "disconnected"



def test_disconnect_retry_resumes_after_remote_revoke(client, monkeypatch, google_on):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    user = _login(client)
    store.set_connection(
        user["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T1"}
    )
    remote_calls: list[str] = []
    monkeypatch.setattr(
        cedric, "revoke_org", lambda org: remote_calls.append(org) or 204
    )
    cleanup_results = iter((False, True))
    monkeypatch.setattr(
        secret_registry, "remove_org_credentials", lambda org: next(cleanup_results)
    )

    first = client.post(
        "/dashboard/connections/brain/disconnect", json={"avatar_id": "cedric"}
    )
    assert first.status_code == 502
    checkpoint = store.connections_for_org(user["org_id"])[0]
    assert checkpoint["status"] == "disconnecting"
    assert checkpoint["config"]["disconnect_phase"] == "remote_revoked"

    second = client.post(
        "/dashboard/connections/brain/disconnect", json={"avatar_id": "cedric"}
    )
    assert second.status_code == 200
    assert remote_calls == [user["org_id"]]
    assert store.connections_for_org(user["org_id"])[0]["status"] == "disconnected"


def test_disconnect_is_org_wide_and_revokes_local_token(
    client, monkeypatch, google_on
):
    monkeypatch.setattr(settings, "cedric_orgs_url", "")
    user = _login(client)
    for avatar in ("laura", "cedric"):
        store.set_connection(
            user["org_id"], avatar, "cedric-brain", "connected", {"team_id": "T1"}
        )
    monkeypatch.setattr(secret_registry, "remove_org_credentials", lambda org: True)
    raw = store.rotate_org_token(user["org_id"], "cedric-slack-install")
    assert store.resolve_org_token(raw) == user["org_id"]

    response = client.post(
        "/dashboard/connections/brain/disconnect", json={"avatar_id": "cedric"}
    )
    assert response.status_code == 200
    assert {
        row["status"] for row in store.connections_for_org(user["org_id"])
        if row["provider"] == "cedric-brain"
    } == {"disconnected"}
    assert store.resolve_org_token(raw) is None


def test_disconnect_tombstone_rejects_old_completion_before_secret_write(
    client, monkeypatch, google_on
):
    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    monkeypatch.setattr(settings, "cedric_orgs_url", "")
    user = _login(client)
    state = install_state.pack(user["org_id"], "cedric", "", "")
    nonce = install_state.unpack(state)["nonce"]
    assert store.begin_brain_install(user["org_id"], "cedric", nonce)

    writes: list[tuple] = []
    monkeypatch.setattr(
        secret_registry,
        "upsert_org_credentials",
        lambda *a: writes.append(a) or True,
    )
    body = {
        "org_id": user["org_id"],
        "avatar_id": "cedric",
        "team_id": "T_GONE",
        "channel": "",
        "webhook_secret": "gone-secret",
        "webhook_token": "gone-peer",
        "state": state,
    }
    completed = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json=body,
    )
    assert completed.status_code == 200
    raw = completed.json()["org_token"]
    monkeypatch.setattr(secret_registry, "remove_org_credentials", lambda org: True)

    disconnected = client.post(
        "/dashboard/connections/brain/disconnect",
        json={"avatar_id": "cedric"},
    )
    assert disconnected.status_code == 200
    writes.clear()

    delayed = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("provisioning-token"),
        json=body,
    )
    assert delayed.status_code == 409
    assert delayed.json() == {"error": "stale install state"}
    assert writes == []
    assert store.resolve_org_token(raw) is None
    row = store.connections_for_org(user["org_id"])[0]
    assert row["status"] == "disconnected"
    assert row["config"]["install_tombstone"] is True
    assert row["config"]["revoked_install_nonce"] == nonce


def test_sqlite_completion_disconnect_race_converges_to_tombstone(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    user = store.upsert_user("sqlite-install-disconnect-race@example.com")
    org = user["org_id"]
    nonce = "sqlite-disconnect-race"
    raw = install_state.derive_org_token(org, nonce)
    assert store.begin_brain_install(org, "cedric", nonce)
    barrier = Barrier(2)

    def complete():
        barrier.wait()
        accepted = store.accept_brain_install(
            org, "cedric", nonce, raw, "T_RACE", "",
            "race-secret", "race-peer",
        )
        finished = (
            store.finish_brain_install(org, "cedric", nonce)
            if accepted in ("applied", "replay")
            else None
        )
        return accepted, finished

    def disconnect():
        barrier.wait()
        assert store.begin_brain_disconnect(org, "cedric", "remote_revoked")
        assert store.tombstone_brain_install(org, "cedric")
        return "disconnected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        completion_future = pool.submit(complete)
        disconnect_future = pool.submit(disconnect)
        completion = completion_future.result()
        assert disconnect_future.result() == "disconnected"

    assert completion[0] in ("applied", "stale")
    assert store.resolve_org_token(raw) is None
    row = store.connections_for_org(org)[0]
    assert row["status"] == "disconnected"
    assert row["config"]["install_tombstone"] is True



def test_endpoint_completion_vs_disconnect_cannot_resurrect_registry(
    client, monkeypatch, google_on
):
    """Completion holds the SQLite saga lock through registry sync; a racing
    disconnect fences afterward and removes the exact same org credentials."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    monkeypatch.setattr(
        settings, "cedric_orgs_url", "https://cedric.example/api/laura/orgs"
    )
    monkeypatch.setattr(cedric, "revoke_org", lambda org: 204)
    user = _login(client)
    start = client.get(
        "/dashboard/connections/brain/slack/start",
        params={"avatar_id": "cedric"},
        follow_redirects=False,
    )
    assert start.status_code == 302
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    payload = {
        "org_id": user["org_id"], "avatar_id": "cedric", "team_id": "T_RACE",
        "channel": "", "webhook_secret": "race-secret",
        "webhook_token": "race-peer", "state": state,
    }
    registry: dict[str, tuple[str, str]] = {}
    registry_lock = Lock()
    registry_entered = Event()
    release_registry = Event()
    disconnect_started = Event()

    def slow_upsert(org, secret, token):
        with registry_lock:
            registry[org] = (secret, token)
        registry_entered.set()
        assert release_registry.wait(10)
        return True

    def remove(org):
        with registry_lock:
            registry.pop(org, None)
        return True

    monkeypatch.setattr(secret_registry, "upsert_org_credentials", slow_upsert)
    monkeypatch.setattr(secret_registry, "remove_org_credentials", remove)
    original_rows = store.connections_for_org

    def disconnect():
        disconnect_started.set()
        return client.post(
            "/dashboard/connections/brain/disconnect",
            json={"avatar_id": "cedric"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(
            client.post,
            "/dashboard/connections/brain/slack/complete",
            headers=_bearer("provisioning-token"),
            json=payload,
        )
        assert registry_entered.wait(10)
        removal = pool.submit(disconnect)
        assert disconnect_started.wait(10)
        release_registry.set()
        completed = completion.result(timeout=10)
        disconnected = removal.result(timeout=10)

    assert completed.status_code == 200
    assert disconnected.status_code == 200
    assert registry == {}
    assert store.resolve_org_token(completed.json()["org_token"]) is None
    row = original_rows(user["org_id"])[0]
    assert row["status"] == "disconnected"
    assert row["config"]["install_tombstone"] is True


def test_endpoint_new_start_makes_old_completion_stale_before_registry_write(
    client, monkeypatch, google_on
):
    """A delayed callback racing a newer OAuth start cannot touch SSM."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setattr(settings, "cedric_orgs_token", "provisioning-token")
    monkeypatch.setattr(
        settings, "cedric_orgs_url", "https://cedric.example/api/laura/orgs"
    )
    user = _login(client)
    old_start = client.get(
        "/dashboard/connections/brain/slack/start",
        params={"avatar_id": "cedric"},
        follow_redirects=False,
    )
    old_state = parse_qs(urlsplit(old_start.headers["location"]).query)["state"][0]
    writes: list[tuple] = []
    monkeypatch.setattr(
        secret_registry, "upsert_org_credentials",
        lambda *a: writes.append(a) or True,
    )
    newer_committed = Event()
    original_begin = store.begin_brain_install

    def observed_begin(*args, **kwargs):
        result = original_begin(*args, **kwargs)
        newer_committed.set()
        return result

    monkeypatch.setattr(store, "begin_brain_install", observed_begin)

    def delayed():
        assert newer_committed.wait(10)
        return client.post(
            "/dashboard/connections/brain/slack/complete",
            headers=_bearer("provisioning-token"),
            json={
                "org_id": user["org_id"], "avatar_id": "cedric",
                "team_id": "T_OLD", "channel": "",
                "webhook_secret": "old-secret", "webhook_token": "old-peer",
                "state": old_state,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        newer_f = pool.submit(
            client.get,
            "/dashboard/connections/brain/slack/start",
            params={"avatar_id": "cedric"},
            follow_redirects=False,
        )
        old_f = pool.submit(delayed)
        newer = newer_f.result(timeout=10)
        old = old_f.result(timeout=10)

    assert newer.status_code == 302
    new_state = parse_qs(urlsplit(newer.headers["location"]).query)["state"][0]
    assert old.status_code == 409
    assert old.json() == {"error": "stale install state"}
    assert writes == []
    row = store.connections_for_org(user["org_id"])[0]
    assert row["config"]["pending_install_nonce"] == install_state.unpack(new_state)["nonce"]


def test_disconnect_rejects_cross_site_and_unknown(client, monkeypatch, google_on):
    user = _login(client)
    store.set_connection(
        user["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T1"}
    )
    cross = client.post(
        "/dashboard/connections/brain/disconnect",
        json={"avatar_id": "cedric"},
        headers={"Origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    missing = client.post(
        "/dashboard/connections/brain/disconnect", json={"avatar_id": "laura"}
    )
    assert missing.status_code == 404  # no brain connection on that avatar
    assert store.connections_for_org(user["org_id"])[0]["status"] == "connected"


def test_remove_org_secret_drops_cache_without_ssm(monkeypatch):
    """Local/dev semantics: no boto3/SSM → the cache drop alone succeeds (the
    env snapshot is not resurrected within this process)."""
    monkeypatch.setattr(secret_registry, "boto3", None)
    monkeypatch.setattr(settings, "laura_webhook_registry_ssm_parameter", "")
    monkeypatch.setattr(secret_registry, "_env_snapshot", "")
    monkeypatch.setattr(secret_registry, "_cache", {"org_x": "s3cret", "org_y": "k"})
    assert secret_registry.remove_org_credentials("org_x") is True
    assert secret_registry.secret_for("org_x") == ""
    assert secret_registry.secret_for("org_y") == "k"
    assert secret_registry.remove_org_credentials("") is False


# ── 4. per-org connection flags for logged-in users ────────────────────

def test_logged_in_fresh_org_reads_tools_not_connected(client, monkeypatch, google_on):
    """The platform's global Google/Slack env is NOT the customer's connection:
    a fresh org reads calendar/gmail/drive/slack disconnected until ITS
    org_connections rows say otherwise."""
    monkeypatch.setattr(settings, "gmail_watch_enabled", True)
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack/x")
    user = _login(client)
    conn = client.get("/dashboard/summary").json()["connections"]
    assert conn["calendar"] is False
    assert conn["gmail"] is False
    assert conn["drive"] is False
    assert conn["slack"] is False

    store.set_connection(user["org_id"], "laura", "calendar", "connected", {})
    store.set_connection(user["org_id"], "laura", "gmail", "connected", {})
    conn = client.get("/dashboard/summary").json()["connections"]
    assert conn["calendar"] is True and conn["gmail"] is True
    assert conn["slack"] is False  # still their org's row, not the global env


def test_anonymous_demo_keeps_global_flags(client, monkeypatch):
    """Key-free demo world unchanged: with the global env set (and login NOT
    enabled), the anonymous dashboard keeps today's global booleans."""
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid-only")
    monkeypatch.setattr(settings, "gmail_watch_enabled", True)
    conn = client.get("/dashboard/summary").json()["connections"]
    assert conn["calendar"] is True
    assert conn["gmail"] is True


def test_global_bearer_is_demo_scoped_not_platform_global(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid-only")
    conn = client.get("/dashboard/summary", headers=_bearer("sesame")).json()[
        "connections"
    ]
    # The deployment bearer is explicitly the Demo tenant, so platform-global
    # OAuth configuration is never presented as that workspace's connection.
    assert conn["calendar"] is False
