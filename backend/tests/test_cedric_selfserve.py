"""Cedric production completion, Laura side (PR D) — key-free.

Proves the four self-serve seams on SQLite with zero external services:

1. PER-ORG machine bearers are first-class on every Cedric-facing endpoint:
   a bearer that resolves via org_tokens acts ONLY on its own org's rows
   (own → ok, another org's → 403); the GLOBAL laura_api_token keeps its
   legacy service scope byte-identically; key-free stays open.
2. /dashboard/connections/brain/slack/complete binds a secret ONLY to an org
   that initiated an install — via the signed state minted by /slack/start
   (echoed back opaquely) or a pending/connected org_connections row. On
   success it returns a freshly minted per-workspace org_token exactly once.
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


def test_org_token_scopes_cancel(client, monkeypatch):
    """A per-org bearer cancels ONLY its own org's sessions; another org's
    stays running and answers a 404 BYTE-IDENTICAL to a nonexistent bot_id —
    no cross-tenant existence oracle (adversarial review should-fix 2). The
    global bearer keeps its full service scope."""
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

        # the global bearer cancels any org's session — today's behavior
        legacy = client.post("/sessions/bot_c_other/cancel", headers=_bearer("sesame"))
        assert legacy.status_code == 200
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
    # global bearer: unchanged full service scope
    legacy = client.get("/sessions/b_art_other/artifact", headers=_bearer("sesame"))
    assert legacy.status_code == 200


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


def test_org_action_status_enforces_org_equality(client, monkeypatch):
    """A per-org bearer may write execution status ONLY for action_ids its
    org owns. Another org's id and an unknown/pre-finalize id both answer the
    IDENTICAL 404 with nothing written (should-fix 3: no status-planting on a
    not-yet-finalized action, no existence oracle). The global bearer keeps
    the trusted service path — pre-finalize writes still land."""
    url = "https://meet.google.com/status-scope"
    ledger.record_meeting(
        url, "laura", "b_own",
        {"actions": [{"owner": "A", "item": "send the doc", "action_id": "aid00000000000a"}]},
        org_id="org_sff",
    )
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    own = store.mint_org_token("org_sff", "svc")
    other = store.mint_org_token("org_other", "svc")

    denied = client.post(
        "/org/actions/aid00000000000a/status",
        headers=_bearer(other),
        json={"status": "done"},
    )
    # pre-finalize/unknown id: a per-org bearer may NOT plant a status
    unknown = client.post(
        "/org/actions/aid_prefinalize00/status",
        headers=_bearer(other),
        json={"status": "done"},
    )
    assert denied.status_code == unknown.status_code == 404
    assert denied.content == unknown.content  # indistinguishable
    assert ledger.items(ledger.meeting_key(url), status="open", org_id="org_sff")
    assert ledger.action_statuses(["aid00000000000a", "aid_prefinalize00"]) == {}

    ok = client.post(
        "/org/actions/aid00000000000a/status",
        headers=_bearer(own),
        json={"status": "done", "detail": "executed"},
    )
    assert ok.status_code == 200
    # the terminal status closed the OWNING org's ledger row (not the demo's)
    assert not ledger.items(ledger.meeting_key(url), status="open", org_id="org_sff")

    # the GLOBAL bearer is the trusted service path: pre-finalize writes work
    svc = client.post(
        "/org/actions/aid_prefinalize00/status",
        headers=_bearer("sesame"),
        json={"status": "proposed"},
    )
    assert svc.status_code == 200
    assert ledger.action_statuses(["aid_prefinalize00"])[
        "aid_prefinalize00"
    ]["status"] == "proposed"


def test_summary_scoped_for_per_org_bearer(client, monkeypatch):
    """/dashboard/summary's machine path: a per-org bearer sees its org + the
    legacy unowned rows — never the Demo org's; the global bearer keeps the
    full service view."""
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
    assert {"own row", "demo row", "legacy row"} <= {
        m["summary"] for m in legacy["meetings"]
    }


# ── 2. slack/complete binds ONLY an initiated install ──────────────────

def test_slack_complete_rejects_uninitiated_org(client, monkeypatch):
    """No signed state: the machine bearer alone cannot bind credentials."""
    monkeypatch.setattr(settings, "laura_api_token", "machine-token")
    user = store.upsert_user("victim@example.com", "Victim")
    writes: list = []
    monkeypatch.setattr(
        secret_registry, "upsert_org_credentials", lambda *a: writes.append(a) or True
    )

    resp = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("machine-token"),
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
    """The full flow: /slack/start (cookie) mints the signed state + a pending
    row → Cedric echoes the state on /complete (bearer) → connected, secret
    bound, and the per-workspace org_token returned exactly once."""
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(settings, "cedric_orgs_token", "shared-test-token")
    monkeypatch.setattr(settings, "laura_api_token", "machine-token")
    user = _login(client)
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        secret_registry,
        "upsert_org_secret",
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

    resp = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("machine-token"),
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
    assert row["config"] == {"team_id": "T_RT", "channel": "#approvals"}
    # the org_token is returned ONCE, resolves to this org, stored hashed only
    token = body["org_token"]
    assert token and store.resolve_org_token(token) == user["org_id"]
    with store._connect() as conn:
        hashes = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM org_tokens")]
    assert token not in hashes


def test_slack_complete_state_alone_proves_initiation(client, monkeypatch):
    """A valid signed state binds the install even if the pending row was lost
    (redeploy wiped the ephemeral store between start and complete)."""
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    monkeypatch.setattr(settings, "laura_api_token", "machine-token")
    user = store.upsert_user("stateful@example.com")
    monkeypatch.setattr(secret_registry, "upsert_org_credentials", lambda *a: True)
    state = install_state.pack(user["org_id"], "cedric", "#ops", "https://x/dash")

    resp = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("machine-token"),
        json={
            "org_id": user["org_id"],
            "avatar_id": "cedric",
            "team_id": "T_ST",
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
    monkeypatch.setattr(settings, "laura_api_token", "machine-token")
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
        headers=_bearer("machine-token"),
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
    monkeypatch.setattr(settings, "laura_api_token", "machine-token")
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
    """With the control plane configured, /complete mirrors the connection and
    mints the DURABLE org token (SQLite stays the fallback)."""
    monkeypatch.setattr(settings, "laura_api_token", "machine-token")
    user = store.upsert_user("durable@example.com")
    store.set_connection(user["org_id"], "cedric", "cedric-brain", "pending", {})
    monkeypatch.setattr(secret_registry, "upsert_org_credentials", lambda *a: True)
    mirrored: list[tuple] = []
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(control_plane, "get_connections", lambda org: [])
    monkeypatch.setattr(
        control_plane,
        "set_connection",
        lambda *a, **k: mirrored.append(a) or True,
    )
    monkeypatch.setattr(
        control_plane, "rotate_org_token", lambda org, label: "durable-raw-token"
    )
    state = install_state.pack(user["org_id"], "cedric", "", "")

    resp = client.post(
        "/dashboard/connections/brain/slack/complete",
        headers=_bearer("machine-token"),
        json={
            "org_id": user["org_id"], "avatar_id": "cedric",
            "team_id": "T_D", "webhook_secret": "s",
            "webhook_token": "peer-d", "state": state,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["org_token"] == "durable-raw-token"
    assert mirrored == [
        (user["org_id"], "cedric", "cedric-brain", "connected",
         {"team_id": "T_D", "channel": ""})
    ]


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
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            calls.append((url, dict(params or {})))
            return _FakeResponse(200, {"org_id": user["org_id"], "connectors": []})

    monkeypatch.setattr(callback.httpx, "Client", FakeClient)
    resp = client.get("/dashboard/connections/brain/connectors")
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    assert calls == [
        (
            "https://cedric/api/laura/connectors",
            {"org_id": user["org_id"], "team": "T_MINE"},
        )
    ]


def test_disconnect_remote_revoke_failure_keeps_local_state(client, monkeypatch, google_on):
    """Remote revoke 5xx → 502, the connection stays 'connected' and the
    signing secret is NOT dropped — never lie about a disconnection."""
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    user = _login(client)
    store.set_connection(
        user["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T1"}
    )
    removed: list = []
    monkeypatch.setattr(
        secret_registry, "remove_org_credentials", lambda org: removed.append(org) or True
    )

    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def delete(self, url, headers=None):
            return _FakeResponse(500)

    monkeypatch.setattr(callback.httpx, "Client", FakeClient)
    resp = client.post(
        "/dashboard/connections/brain/disconnect", json={"avatar_id": "cedric"}
    )
    assert resp.status_code == 502
    assert resp.json() == {"error": "remote_revoke_failed"}
    assert removed == []
    row = store.connections_for_org(user["org_id"])[0]
    assert row["status"] == "connected"  # unchanged


def test_disconnect_remote_revoke_ok_drops_secret(client, monkeypatch, google_on):
    monkeypatch.setattr(settings, "cedric_orgs_url", "https://cedric/api/laura/orgs")
    user = _login(client)
    store.set_connection(
        user["org_id"], "cedric", "cedric-brain", "connected", {"team_id": "T1"}
    )
    removed: list = []
    monkeypatch.setattr(
        secret_registry, "remove_org_credentials", lambda org: removed.append(org) or True
    )
    deleted: list[str] = []

    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def delete(self, url, headers=None):
            deleted.append(url)
            return _FakeResponse(204)

    monkeypatch.setattr(callback.httpx, "Client", FakeClient)
    resp = client.post(
        "/dashboard/connections/brain/disconnect", json={"avatar_id": "cedric"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disconnected"
    assert resp.json()["remote_revoked"] is True
    assert deleted == [f"https://cedric/api/laura/orgs/{user['org_id']}"]
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


def test_global_bearer_keeps_global_flags(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid-only")
    conn = client.get("/dashboard/summary", headers=_bearer("sesame")).json()[
        "connections"
    ]
    assert conn["calendar"] is True
