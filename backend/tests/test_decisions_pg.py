"""First-class Decision records on real Postgres (migration 0017).

Runs ``alembic upgrade head`` against an embedded Postgres as the unprivileged
``laura_app`` role and asserts the 0017 boundary holds:

* ``public.meeting_decisions`` exists with FORCE ROW LEVEL SECURITY;
* ``laura_app`` has SELECT/INSERT/UPDATE but NOT DELETE (decisions are
  superseded, never deleted by the runtime);
* the control-plane DAL (save/list/get/mark_superseded) round-trips under RLS;
* cross-org isolation: one tenant never sees another's decisions.

Auto-skipped when pgserver isn't installed. No decision text is logged.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import control_plane, store  # noqa: E402
from app.config import settings  # noqa: E402

pytestmark = pytest.mark.pg
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_ROLE = "laura_app"


def _write_extension_shims() -> None:
    ext_dir = (
        Path(pgserver.__file__).parent
        / "pginstall" / "share" / "postgresql" / "extension"
    )
    shims = {
        "pgcrypto.control": (
            "default_version = '1.0'\nrelocatable = true\n"
            "comment = 'test shim'\n"
        ),
        "pgcrypto--1.0.sql": "-- shim\n",
        "citext.control": (
            "default_version = '1.0'\nrelocatable = true\n"
            "comment = 'test shim'\n"
        ),
        "citext--1.0.sql": "CREATE DOMAIN citext AS text;\n",
    }
    for name, body in shims.items():
        path = ext_dir / name
        if not path.exists():
            path.write_text(body)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    _write_extension_shims()
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("dec_pg")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'pw' "
            "NOSUPERUSER NOBYPASSRLS"
        )
    import psycopg.conninfo as ci
    from sqlalchemy.engine import URL

    admin_sa_url = uri.replace("postgresql://", "postgresql+psycopg://")
    info = ci.conninfo_to_dict(uri)
    query = {k: str(info[k]) for k in ("host", "port")
             if info.get(k) is not None}
    app_sa_url = URL.create(
        "postgresql+psycopg", username=APP_ROLE, password="pw",
        database=info.get("dbname"), query=query,
    ).render_as_string(hide_password=False)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={**os.environ, "LAURA_DATABASE_ADMIN_URL": admin_sa_url,
             "LAURA_DATABASE_URL": "", "LAURA_REQUIRE_MIGRATIONS": "1"},
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, (
        f"alembic upgrade failed:\n{proc.stdout}\n{proc.stderr}"
    )
    yield {"uri": uri, "app_sa_url": app_sa_url}
    control_plane.reset_engine()
    srv.cleanup()


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


@pytest.fixture
def cp(pg, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    control_plane.reset_engine()
    with _admin(pg) as conn:
        conn.execute("DELETE FROM public.meeting_decisions")
    yield control_plane
    control_plane.reset_engine()


def _org(cp, tag: str) -> str:
    stamp = str(time.time_ns())
    return cp.ensure_user(
        f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test", tag, ""
    )["org_id"]


# ── the table + RLS + grants exist ──────────────────────────────────────────
def test_table_and_force_rls_exist(pg):
    with _admin(pg) as conn:
        row = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'public.meeting_decisions'::regclass"
        ).fetchone()
    assert row == (True, True), "meeting_decisions must FORCE row level security"


def test_tenant_isolation_policy_present(pg):
    with _admin(pg) as conn:
        pol = conn.execute(
            "SELECT polname FROM pg_policy "
            "WHERE polrelid = 'public.meeting_decisions'::regclass"
        ).fetchall()
    assert ("tenant_isolation",) in pol


def test_laura_app_grants_no_delete(pg):
    with _admin(pg) as conn:
        def has(priv: str) -> bool:
            return conn.execute(
                "SELECT has_table_privilege(%s, 'public.meeting_decisions', %s)",
                (APP_ROLE, priv),
            ).fetchone()[0]

        assert has("SELECT")
        assert has("INSERT")
        assert has("UPDATE")
        assert not has("DELETE"), "runtime must not be able to DELETE decisions"


# ── DAL round-trip + RLS isolation ──────────────────────────────────────────
def test_control_plane_decision_roundtrip(cp):
    org = _org(cp, "roundtrip")
    did = cp.save_decision(
        org,
        {
            "id": str(uuid.uuid4()),
            "bot_id": "bot_pg",
            "decision": "Adopt Postgres for the control plane",
            "decision_maker": "Priya",
            "reason": "RLS",
            "related_project": "Data platform",
            "status": "active",
            "source_ref": "bot_pg",
        },
    )
    assert did

    rows = cp.list_decisions(org, "bot_pg")
    assert len(rows) == 1
    assert rows[0]["decision"] == "Adopt Postgres for the control plane"
    assert rows[0]["decision_maker"] == "Priya"
    assert rows[0]["related_project"] == "Data platform"

    got = cp.get_decision(org, did)
    assert got is not None and got["id"] == did

    assert cp.mark_superseded(org, did) is True
    assert cp.get_decision(org, did)["status"] == "superseded"


def test_supersede_link_via_store_dal(cp, monkeypatch):
    """store.persist_decision_records routes durable orgs to Postgres and wires
    the supersede link there."""
    # durable_artifacts_enabled() requires laura_database_url set (cp did) —
    # the store DAL then routes to control_plane for a durable org.
    org = _org(cp, "supersede")
    old_id = store.save_decision(
        org, "bot_old",
        {"decision": "Go with MySQL", "related_project": "DB choice"},
    )
    new_ids = store.persist_decision_records(
        org, "bot_new",
        [{"decision": "Superseding MySQL, moving to Postgres",
          "related_project": "DB choice"}],
    )
    assert len(new_ids) == 1
    assert store.get_decision(org, new_ids[0])["supersedes"] == old_id
    assert store.get_decision(org, old_id)["status"] == "superseded"


def test_cross_org_isolation(cp):
    org_a = _org(cp, "iso-a")
    org_b = _org(cp, "iso-b")
    cp.save_decision(org_a, {
        "id": str(uuid.uuid4()),
        "bot_id": "b", "decision": "A's private decision", "status": "active",
    })
    assert cp.list_decisions(org_a) and len(cp.list_decisions(org_a)) == 1
    # org_b's RLS-scoped read never sees org_a's row
    assert cp.list_decisions(org_b) == []
