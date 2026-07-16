"""Per-org agents — the org_id seam made real (SFF Studio).

Key-free like the rest of the suite: sqlite in tmp_path, no vendors touched.
The property under test: a member of a REAL org (verified corporate domain)
resolves to that org and sees ONLY its granted agents, while personal-org /
demo / unknown callers keep today's all-avatars behavior (backward-compatible,
key-free demo unchanged).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import avatars, ledger, store
from app.config import settings


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """A clean sqlite store (re-seeded on reload) pointed at tmp_path."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)  # shares the sqlite file — needs its tables too
    yield store
    importlib.reload(store)
    importlib.reload(ledger)


def _expected_sff_agents() -> list[str]:
    """The seeded SFF grants that still exist as folders, sorted — the expected
    subset of ["cedric", "laura"] (skips any grant whose folder is gone)."""
    installed = set(avatars.list_ids())
    return sorted(a for a in ("cedric", "laura") if a in installed)


def test_sff_login_resolves_org_and_scopes_agents(fresh_store, monkeypatch):
    store = fresh_store
    # Personal-first is the default (2026-07-16); the shared-domain grouping
    # this test exercises is the parked "teams" feature behind the flag.
    from app.config import settings

    monkeypatch.setattr(settings, "shared_domain_orgs", True)
    user = store.upsert_user(email="ceo@sffstudio.com", name="SFF CEO")

    # With shared-domain routing ON, a verified corporate domain resolves to
    # the shared org (NOT the personal per-user org).
    assert user["org_id"] == "org_sff"
    assert user["org_id"] != user["user_id"]

    # An active membership row now links the user to the org.
    with store._connect() as conn:
        row = conn.execute(
            "SELECT role, status FROM memberships WHERE user_id=? AND org_id=?",
            (user["user_id"], "org_sff"),
        ).fetchone()
    assert row is not None
    assert row["role"] == "member" and row["status"] == "active"


def test_personal_first_default_ignores_verified_domain(fresh_store):
    """Default (flag off): two colleagues on the verified domain each get their
    OWN org — the whole point of personal-first."""
    store = fresh_store
    a = store.upsert_user(email="ceo@sffstudio.com", name="SFF CEO")
    b = store.upsert_user(email="cfo@sffstudio.com", name="SFF CFO")
    assert a["org_id"] == a["user_id"] and b["org_id"] == b["user_id"]
    assert a["org_id"] != b["org_id"] != "org_sff"

    # The org sees ONLY its granted agents.
    expected = _expected_sff_agents()
    assert expected == ["cedric", "laura"]  # both folders exist in this repo
    assert avatars.list_for_org("org_sff") == expected
    assert set(avatars.list_for_org("org_sff")) < set(avatars.list_ids())


def test_gmail_login_is_personal_org_all_avatars(fresh_store):
    store = fresh_store
    user = store.upsert_user(email="someone@gmail.com")

    # Free-mail domain is never mapped: personal org, org_id == user_id.
    assert user["org_id"] == user["user_id"]
    # A personal org has no grants → sees every installed avatar (unchanged).
    assert avatars.list_for_org(user["org_id"]) == avatars.list_ids()

    # No membership row is created for a personal org.
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT 1 FROM memberships WHERE user_id=?", (user["user_id"],)
        ).fetchall()
    assert rows == []


def test_demo_and_unknown_orgs_see_all_avatars(fresh_store):
    all_avatars = avatars.list_ids()
    assert avatars.list_for_org(settings.demo_org_id) == all_avatars
    assert avatars.list_for_org("unknown-org") == all_avatars
    assert avatars.list_for_org("") == all_avatars


def test_seed_is_idempotent(fresh_store):
    store = fresh_store
    # _init_db already seeded once on reload; re-running must not error/duplicate.
    store.seed_builtin_orgs()
    store.seed_builtin_orgs()

    with store._connect() as conn:
        orgs = conn.execute(
            "SELECT COUNT(*) c FROM orgs WHERE id='org_sff'"
        ).fetchone()["c"]
        agents = conn.execute(
            "SELECT COUNT(*) c FROM org_agents WHERE org_id='org_sff'"
        ).fetchone()["c"]
        domains = conn.execute(
            "SELECT COUNT(*) c FROM org_domains WHERE org_id='org_sff'"
        ).fetchone()["c"]

    assert orgs == 1
    assert agents == len(_expected_sff_agents())
    assert agents <= 2
    assert domains == 1


def test_seed_gate_disables_seed(tmp_path, monkeypatch):
    """With settings.seed_builtin_orgs False, no org is seeded and a corporate
    login falls back to a personal org (no domain map to resolve against)."""
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "seed_off.sqlite3"))
    monkeypatch.setattr(settings, "seed_builtin_orgs", False)
    importlib.reload(store)
    importlib.reload(ledger)
    try:
        with store._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM orgs WHERE id='org_sff'"
            ).fetchone()["c"]
        assert n == 0
        user = store.upsert_user(email="ceo@sffstudio.com")
        assert user["org_id"] == user["user_id"]  # personal fallback
    finally:
        importlib.reload(store)
        importlib.reload(ledger)


def test_demo_roster_endpoint_lists_all_avatars(fresh_store):
    """Regression: the anonymous /avatars demo picker still lists every
    installed avatar (key-free demo unchanged)."""
    client = TestClient(main_module.app)
    ids = {a["id"] for a in client.get("/avatars").json()["avatars"]}
    assert ids == set(avatars.list_ids())


def test_org_exists_shared_vs_personal(fresh_store):
    """Regression for the brain-connect blocker: a SHARED org (org_sff) is a row
    in `orgs`, never in `users`, so validating it with get_user alone 404s it.
    org_exists must recognise the shared org while get_user does not."""
    store = fresh_store
    user = store.upsert_user(email="ceo@sffstudio.com")

    # The exact asymmetry the blocker hinged on:
    assert store.get_user("org_sff") is None  # old check would 404 the connect flow
    assert store.org_exists("org_sff") is True  # new check accepts the shared org

    # A personal org is a users row, not an orgs row.
    assert store.get_user(user["user_id"]) is not None
    assert store.org_exists(user["user_id"]) is False
    # Unknown / empty are neither.
    assert store.org_exists("unknown-org") is False
    assert store.org_exists("") is False


def test_list_for_org_fails_open_when_grants_dangle(fresh_store):
    """If an org's grants all reference missing folders (e.g. a folder rename
    without a seed update), the org must fall back to ALL avatars — never a
    dead, empty dashboard."""
    store = fresh_store
    with store._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO orgs (id, name, slug, created_at) "
            "VALUES ('org_ghost', 'Ghost', 'ghost', 0)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO org_agents (org_id, avatar_id) "
            "VALUES ('org_ghost', 'no_such_avatar')"
        )
    # Grants exist but none resolve → fail open to all avatars (not []).
    assert avatars.list_for_org("org_ghost") == avatars.list_ids()
    assert avatars.list_for_org("org_ghost") != []


def test_seed_skipped_and_swept_when_control_plane_enabled(fresh_store, monkeypatch):
    """DURABLE deployments: the SQLite org_sff seed is a non-uuid shadow tenant
    no durable path can serve (billing/entitlements cast org_id to uuid) — with
    the control plane enabled, seed_builtin_orgs must not mint it AND must
    sweep rows left by an earlier boot / restored replica (idempotent)."""
    store = fresh_store
    # The fixture reload already seeded org_sff (control plane off) — the rows
    # a Litestream-restored prod replica would carry.
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM orgs WHERE id='org_sff'"
        ).fetchone()["c"] == 1

    # Signal "durable control plane configured" the way the code actually
    # checks it — settings.laura_database_url (NOT control_plane.enabled(),
    # which store must not import at init: see the cycle note in store.py).
    monkeypatch.setattr(settings, "laura_database_url", "postgres://ci-not-connected")
    store.seed_builtin_orgs()  # sweep
    store.seed_builtin_orgs()  # idempotent — second run is a no-op

    with store._connect() as conn:
        for table, col in (("orgs", "id"), ("org_domains", "org_id"), ("org_agents", "org_id")):
            n = conn.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE {col}='org_sff'"
            ).fetchone()["c"]
            assert n == 0, f"{table} still carries org_sff"
        # The Demo org (a uuid, load-bearing for key-free sessions) survives.
        assert conn.execute(
            "SELECT COUNT(*) c FROM orgs WHERE id=?", (settings.demo_org_id,)
        ).fetchone()["c"] == 1
    # With the control plane back OFF (a real durable login would need Postgres),
    # a corporate login now falls back to the personal org — the swept domain
    # row no longer maps it.
    monkeypatch.setattr(settings, "laura_database_url", "")
    user = store.upsert_user(email="ceo@sffstudio.com")
    assert user["org_id"] == user["user_id"]
