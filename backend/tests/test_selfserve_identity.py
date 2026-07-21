"""Self-serve identity/control-plane seams; the KEY-FREE half (PR A).

Everything here runs on SQLite with zero external services, proving:
- control_plane is a strict no-op when LAURA_DATABASE_URL is empty (the
  key-free demo contract; no engine, every function None/no-op);
- store.mint_org_token / resolve_org_token (the SQLite fallback of the per-org
  machine bearer) round-trip, and /sessions/start scopes a token-bearing
  service start to the token's org; never to a body field;
- internal avatars (settings.internal_avatar_ids, default 'duccio') are
  invisible to every roster and refused by dispatch for every caller;
- the demo-org sentinel flip (self-serve product decision, 2026-07-13): a
  logged-in user's allow-set is ('', their org); demo rows are the anonymous
  showroom and never leak into a real signup's dashboard/archive/redeliver.

The Postgres half (real RLS, two-org isolation) lives in
test_control_plane_pg.py.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, avatars, control_plane, ledger, store
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file; needs its tables too
    return TestClient(main_module.app)


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setattr(settings, "google_calendar_client_id", "cid-test")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "csecret-test")


@pytest.fixture
def stub_recall(monkeypatch):
    from app import recall_client

    monkeypatch.setattr(recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(
        recall_client, "create_bot", lambda *a, **k: {"id": "bot_token_test"}
    )


def _login(client, email, name="Test User") -> dict:
    user = store.upsert_user(email=email, name=name)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


# ── control_plane: strict no-op when LAURA_DATABASE_URL is empty ───────

def test_control_plane_disabled_is_total_noop():
    """The key-free contract: with an empty URL nothing runs, nothing connects
    , every public function returns None (and no engine is ever created)."""
    assert settings.laura_database_url == ""  # pinned by _keyfree_settings
    control_plane.reset_engine()
    assert control_plane.enabled() is False
    assert control_plane.ensure_user("sub", "a@b.c", "A", "") is None
    assert control_plane.resolve_org_token("anything") is None
    assert control_plane.mint_org_token("org", "label") is None
    assert control_plane.set_connection("org", "laura", "slack", "connected") is None
    assert control_plane.get_connections("org") is None
    assert control_plane.org_plan("org") is None
    assert control_plane._get_engine() is None
    assert control_plane._engine is None  # nothing was lazily created



def test_key_free_lifespan_skips_runtime_role_verification(monkeypatch):
    assert control_plane.enabled() is False

    def must_not_run():
        raise AssertionError("key-free startup must not touch the control plane")

    monkeypatch.setattr(control_plane, "runtime_role_status", must_not_run)
    monkeypatch.setattr(main_module, "_prebuild_indexes", lambda: None)
    monkeypatch.setattr(settings, "autopilot_nudge", False)
    monkeypatch.setattr(settings, "gmail_watch_enabled", False)
    monkeypatch.setattr(settings, "vendor_alerts_enabled", False)
    monkeypatch.setattr(settings, "reconcile_enabled", False)
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    main_module._shutting_down = False
    try:
        with TestClient(main_module.app) as startup_client:
            assert startup_client.get("/health").status_code == 200
        assert control_plane._engine is None
    finally:
        # Lifespan shutdown sets the process drain flag; do not contaminate the
        # rest of the key-free test process.
        main_module._shutting_down = False

def test_upsert_user_unchanged_when_control_plane_off(client):
    """Login resolution with the control plane off is exactly today's:
    personal org == user_id, google_sub accepted but unused."""
    user = store.upsert_user("solo@example.com", google_sub="sub-123")
    assert user["org_id"] == user["user_id"]


def test_control_plane_failure_rejects_login_without_local_tenant(client, monkeypatch):
    """A durable-control-plane outage cannot mint a parallel local org."""
    monkeypatch.setattr(control_plane, "enabled", lambda: True)

    def unavailable(*args, **kwargs):
        raise ValueError("synthetic database outage")

    monkeypatch.setattr(control_plane, "ensure_user", unavailable)
    with pytest.raises(RuntimeError, match="durable identity is temporarily unavailable"):
        store.upsert_user("failclosed@example.com", google_sub="sub-failclosed")
    with store._connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM users WHERE email = ?",
            ("failclosed@example.com",),
        ).fetchone()[0]
    assert count == 0


# ── per-org machine tokens (SQLite fallback) ───────────────────────────

def test_store_org_token_roundtrip(client):
    raw = store.mint_org_token("org_sff", "ci-test")
    assert raw and isinstance(raw, str)
    assert store.resolve_org_token(raw) == "org_sff"
    # the raw token is never stored: only its hash is in the table
    with store._connect() as conn:
        rows = conn.execute("SELECT token_hash FROM org_tokens").fetchall()
    assert all(raw != r["token_hash"] for r in rows)
    assert store.resolve_org_token("not-a-token") is None
    assert store.resolve_org_token("") is None
    assert store.mint_org_token("") is None


def test_session_start_scopes_to_org_token(client, stub_recall):
    """A machine caller with a PER-ORG bearer dispatches into ITS org; the
    service twin of the cookie principal (never a request-body field:
    StartRequest has no org field by design)."""
    raw = store.mint_org_token("org_sff", "svc")
    resp = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/tok"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200, resp.text
    try:
        assert store.get("bot_token_test").org_id == "org_sff"
    finally:
        store.remove("bot_token_test")


def test_org_token_authenticates_when_login_and_token_enabled(
    client, google_on, stub_recall, monkeypatch
):
    """On a locked deployment (global token + Google login), a valid org token
    must both AUTHENTICATE the start and scope it; without it the same call
    is a 401."""
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    denied = client.post(
        "/sessions/start", json={"meeting_url": "https://meet.google.com/tok2"}
    )
    assert denied.status_code == 401

    raw = store.mint_org_token("org_sff", "svc")
    resp = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/tok2"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200, resp.text
    try:
        assert store.get("bot_token_test").org_id == "org_sff"
    finally:
        store.remove("bot_token_test")


def test_global_bearer_keeps_demo_org(client, stub_recall, monkeypatch):
    """The GLOBAL laura_api_token bearer keeps today's behavior: service
    starts land in the Demo org, not in any org_tokens row."""
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    resp = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/glob"},
        headers={"Authorization": "Bearer sesame"},
    )
    assert resp.status_code == 200, resp.text
    try:
        assert store.get("bot_token_test").org_id == settings.demo_org_id
    finally:
        store.remove("bot_token_test")


# ── internal avatars: invisible + undispatchable ───────────────────────

def test_internal_avatar_hidden_from_all_rosters(client):
    assert avatars.is_internal("duccio") is True
    assert "duccio" not in avatars.list_ids()
    assert "duccio" not in avatars.list_for_org("")
    assert "duccio" not in avatars.list_for_org(settings.demo_org_id)
    ids = {a["id"] for a in client.get("/avatars").json()["avatars"]}
    assert "duccio" not in ids
    # the folder itself still exists and load() still works (legacy/direct
    # uses); only listing + dispatch are gated
    assert (settings.avatars_dir / "duccio" / "avatar.yaml").exists()
    assert avatars.load("duccio").id


def test_internal_avatar_dispatch_refused_for_every_caller(
    client, stub_recall, monkeypatch
):
    """/sessions/start answers 404 unknown avatar for an internal id, for the
    anonymous demo caller, the global bearer, AND a logged-in user."""
    # anonymous (key-free demo)
    resp = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/i1", "avatar_id": "duccio"},
    )
    assert resp.status_code == 404
    # global machine bearer
    monkeypatch.setattr(settings, "laura_api_token", "sesame")
    resp = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/i2", "avatar_id": "duccio"},
        headers={"Authorization": "Bearer sesame"},
    )
    assert resp.status_code == 404
    monkeypatch.setattr(settings, "laura_api_token", "")
    # logged-in user
    _login(client, "alice@example.com")
    resp = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/i3", "avatar_id": "duccio"},
    )
    assert resp.status_code == 404
    assert store.get("bot_token_test") is None  # nothing was dispatched


def test_internal_avatar_refused_on_service_paths():
    """_start_avatar_session (shared by calendar auto-join + Gmail watcher)
    raises like an unknown folder, so no service path can summon an internal
    persona either. (The guard fires before the first await, so asyncio.run
    surfaces it without any vendor stubbing.)"""
    import asyncio

    with pytest.raises(FileNotFoundError):
        asyncio.run(
            main_module._start_avatar_session("https://meet.google.com/svc", "duccio")
        )


def test_internal_guard_is_config_driven(client, monkeypatch):
    """Emptying INTERNAL_AVATAR_IDS restores the old behavior; the guard is
    an env knob, not a hardcoded id."""
    monkeypatch.setattr(settings, "internal_avatar_ids", "")
    assert avatars.is_internal("duccio") is False
    assert "duccio" in avatars.list_ids()


# ── the demo-org sentinel flip (product decision 2026-07-13) ───────────

def test_dashboard_hides_demo_org_rows_from_logged_in_user(client, google_on):
    """A logged-in user's dashboard shows their org + legacy unowned ('') rows
    , never the Demo org's (each anonymous visitor's showroom meetings would
    otherwise appear in every real signup's dashboard)."""
    alice = _login(client, "alice@example.com")
    store.save_artifact(
        "b_demo",
        {"summary": "demo row", "org_id": settings.demo_org_id},
        org_id=settings.demo_org_id,
    )
    store.save_artifact("b_mine", {"summary": "my row", "org_id": alice["org_id"]})
    store.save_artifact("b_legacy", {"summary": "legacy row", "org_id": ""})
    store.create("bot_demo_live", "https://meet.google.com/dl", "laura")  # demo org
    try:
        data = client.get("/dashboard/summary").json()
        summaries = {m["summary"] for m in data["meetings"]}
        assert summaries == {"my row", "legacy row"}
        assert {s["bot_id"] for s in data["live"]} == set()  # demo live hidden
    finally:
        store.remove("bot_demo_live")


def test_redeliver_refuses_demo_org_artifact_for_logged_in_user(
    client, google_on, monkeypatch
):
    """Same allow-set on /sessions/{id}/redeliver: a demo-org artifact is not
    the logged-in user's to redeliver (404, indistinguishable from missing)."""
    monkeypatch.setattr(settings, "surface_webhook_url", "https://cedric/api/laura/events")
    _login(client, "alice@example.com")
    store.save_artifact(
        "b_demo_rd",
        {"summary": "s", "org_id": settings.demo_org_id},
        org_id=settings.demo_org_id,
    )
    resp = client.post("/sessions/b_demo_rd/redeliver")
    assert resp.status_code == 404


def test_anonymous_demo_caller_unchanged(client):
    """The anonymous/demo world is untouched by the sentinel flip: with no
    login configured, demo-org rows are fully visible (key-free demo)."""
    store.save_artifact(
        "b_demo_anon",
        {"summary": "anon demo", "org_id": settings.demo_org_id},
        org_id=settings.demo_org_id,
    )
    data = client.get("/dashboard/summary").json()
    assert "anon demo" in {m["summary"] for m in data["meetings"]}
    bots = {m["bot_id"] for m in client.get("/meetings/list").json()["meetings"]}
    assert "b_demo_anon" in bots


# ── cutover backfill (adversarial-review blocker 2, 2026-07-13) ────────

def test_cutover_restamps_personal_rows_to_durable_org(client, monkeypatch):
    """When the control plane flips on, a returning user's org changes
    u_<hash> → UUID; their pre-cutover rows (artifacts/sessions stamped with
    the personal u_<hash> org) must be re-stamped in the same login, or the
    owner's own history silently disappears from the new org's visibility
    set. Artifacts need BOTH the column and the JSON org_id moved (the
    dashboard/meetings filters read the JSON). Idempotent; other tenants'
    rows untouched."""
    import uuid as _uuid

    email = "mover@example.com"
    first = store.upsert_user(email)  # pre-cutover login: personal u_<hash> org
    old_org = first["org_id"]
    assert old_org == first["user_id"]
    store.save_artifact("b_move", {"summary": "old meeting", "org_id": old_org})
    store.create("bot_move", "https://meet.google.com/mv", "laura", org_id=old_org)
    store.save_artifact("b_other", {"summary": "other", "org_id": "org_other"})

    durable_org = str(_uuid.uuid4())
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(
        control_plane,
        "ensure_user",
        lambda sub, em, nm, pic: {
            "user_id": "ignored-uuid", "org_id": durable_org,
            "email": em, "created": False,
        },
    )
    try:
        user = store.upsert_user(email, google_sub="sub-mover")
        assert user["org_id"] == durable_org

        rows = store.list_artifacts(org_id=durable_org)
        assert [r["bot_id"] for r in rows] == ["b_move"]
        # the JSON field too. /meetings/list + dashboard scope on it
        assert rows[0]["artifact"]["org_id"] == durable_org
        assert store.get_artifact("b_move")["org_id"] == durable_org  # memory cache
        assert store.get("bot_move").org_id == durable_org  # live session (memory)
        with store._connect() as conn:
            db_org = conn.execute(
                "SELECT org_id FROM sessions WHERE bot_id='bot_move'"
            ).fetchone()["org_id"]
        assert db_org == durable_org
        # another tenant's rows are untouched
        assert store.get_artifact("b_other")["org_id"] == "org_other"

        # idempotent: the next login re-stamps nothing and keeps the org
        again = store.upsert_user(email, google_sub="sub-mover")
        assert again["org_id"] == durable_org
        assert store.get_artifact("b_move")["org_id"] == durable_org
    finally:
        store.remove("bot_move")


def test_cutover_never_restamps_shared_org_rows(client, monkeypatch):
    """The re-stamp fires ONLY when the previous org was the user's own
    personal u_<hash> org. Rows of a SHARED (verified-domain) org belong to
    the org, not the person; a member's cutover must not drag them along."""
    import uuid as _uuid

    # Shared orgs only exist under the parked flag now (personal-first default,
    # 2026-07-16); this test is specifically about NOT restamping a shared org.
    monkeypatch.setattr(settings, "shared_domain_orgs", True)
    email = "ceo@sffstudio.com"  # seeded verified domain → org_sff
    first = store.upsert_user(email)
    assert first["org_id"] == "org_sff"
    store.save_artifact("b_sff", {"summary": "org meeting", "org_id": "org_sff"})

    durable_org = str(_uuid.uuid4())
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(
        control_plane,
        "ensure_user",
        lambda sub, em, nm, pic: {
            "user_id": "ignored-uuid", "org_id": durable_org,
            "email": em, "created": False,
        },
    )
    user = store.upsert_user(email, google_sub="sub-ceo")
    assert user["org_id"] == durable_org
    assert store.get_artifact("b_sff")["org_id"] == "org_sff"  # untouched


# ── org-token end/redeliver symmetry (should-fix A, 2026-07-13) ────────

def test_org_token_can_end_only_its_own_org_sessions(client, stub_recall, monkeypatch):
    """The per-org bearer that can START a session can also END it (meter
    symmetry, PR D); but ONLY sessions of ITS org; demo-org and other-org
    sessions answer 404 and stay running without becoming an existence oracle."""
    from app import recall_client

    monkeypatch.setattr(recall_client, "leave_call", lambda bot_id: None)
    raw = store.mint_org_token("org_sff", "svc")
    headers = {"Authorization": f"Bearer {raw}"}

    # its own org's session: endable
    store.create("bot_own", "https://meet.google.com/own", "laura", org_id="org_sff")
    resp = client.post("/sessions/bot_own/end", headers=headers)
    assert resp.status_code in (200, 202), resp.text
    if store.get("bot_own"):
        store.remove("bot_own")

    # another org's session: 404, session left running
    store.create("bot_theirs", "https://meet.google.com/th", "laura", org_id="org_other")
    try:
        resp = client.post("/sessions/bot_theirs/end", headers=headers)
        assert resp.status_code == 404
        assert store.get("bot_theirs") is not None
    finally:
        store.remove("bot_theirs")

    # the demo org's session: 404 too (never demo/other)
    store.create("bot_demo_tok", "https://meet.google.com/dm", "laura")
    try:
        resp = client.post("/sessions/bot_demo_tok/end", headers=headers)
        assert resp.status_code == 404
        assert store.get("bot_demo_tok") is not None
    finally:
        store.remove("bot_demo_tok")


def test_org_token_can_redeliver_only_its_own_org_artifacts(client, monkeypatch):
    """Redeliver half of should-fix A: a per-org bearer retries ONLY its own
    org's artifact callbacks."""
    monkeypatch.setattr(
        settings, "surface_webhook_url", "https://cedric.example/api/laura/events"
    )
    delivered: list[str] = []
    monkeypatch.setattr(
        main_module.cedric,
        "deliver_ended",
        lambda integration, bot_id, artifact: delivered.append(bot_id) or True,
    )
    store.save_artifact("b_rd_own", {"summary": "s", "org_id": "org_sff"})
    store.save_artifact("b_rd_other", {"summary": "s", "org_id": "org_other"})
    raw = store.mint_org_token("org_sff", "svc")
    headers = {"Authorization": f"Bearer {raw}"}

    ok = client.post("/sessions/b_rd_own/redeliver", headers=headers)
    assert ok.status_code == 202, ok.text
    assert delivered == ["b_rd_own"]

    denied = client.post("/sessions/b_rd_other/redeliver", headers=headers)
    assert denied.status_code == 404
    assert delivered == ["b_rd_own"]  # nothing new was scheduled
