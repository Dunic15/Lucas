"""Meeting Memory — RLS + permission-safe search on real Postgres as laura_app.

Durable indexing, the default-deny visibility predicate enforced in SQL, the
raw-transcript-exclusion invariant at rest (the table has no transcript
column), and cross-org isolation both through the API and at the RLS backstop.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import control_plane, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.meeting import meeting_memory  # noqa: E402

pytestmark = pytest.mark.pg
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_ROLE = "laura_app"

_TRANSCRIPT = "Sam: provision ACME SECRETSAUCE-do-not-index\nLaura: after approval"


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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("mm_pg")))
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
    app_uri = ci.make_conninfo(
        dbname=info.get("dbname"), user=APP_ROLE, password="pw",
        host=info.get("host"), port=info.get("port"),
    )
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
    yield {"uri": uri, "app_sa_url": app_sa_url, "app_uri": app_uri}
    control_plane.reset_engine()
    srv.cleanup()


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


@pytest.fixture
def cp(pg, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    monkeypatch.setattr(settings, "meeting_memory_enabled", True)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    meeting_memory._SQLITE_READY = False
    control_plane.reset_engine()
    with _admin(pg) as conn:
        conn.execute("DELETE FROM meeting_memory")
    yield control_plane
    meeting_memory._SQLITE_READY = False
    control_plane.reset_engine()


def _org(cp, tag: str) -> str:
    stamp = str(time.time_ns())
    return cp.ensure_user(
        f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test", tag, ""
    )["org_id"]


def _artifact(**over) -> dict:
    art = {
        "avatar_id": "laura",
        "meeting_type": "Onboarding",
        "summary": "We onboarded ACME and assigned next steps.",
        "decisions": ["Approved ACME provisioning"],
        "actions": [{"action": "Provision ACME", "owner": "Sam"}],
        "visibility": "org",
        "principal_id": "u_runner",
        "transcript": _TRANSCRIPT,
    }
    art.update(over)
    return art


def test_durable_index_and_search(cp):
    org = _org(cp, "mmidx")
    meeting_memory.index_artifact(org, "bot-1", _artifact(),
                                  meeting_date=1_700_000_000.0)
    results = meeting_memory.search(org, "ACME provisioning",
                                    principal_ref="u_runner")
    assert [r["meeting_id"] for r in results] == ["bot-1"]
    # Durable rows carry NO transcript — not in the result, not at rest.
    assert "SECRETSAUCE-do-not-index" not in str(results)


def test_durable_permission_default_deny(cp):
    org = _org(cp, "mmdeny")
    meeting_memory.index_artifact(
        org, "bot-p", _artifact(visibility="participants",
                                principal_id="u_runner"),
        meeting_date=1.0,
    )
    assert meeting_memory.search(org, "ACME", principal_ref="u_runner")
    assert meeting_memory.search(org, "ACME", principal_ref="u_other") == []
    assert meeting_memory.search(org, "ACME", principal_ref="") == []


def test_no_transcript_column_at_rest(cp, pg):
    org = _org(cp, "mmrest")
    meeting_memory.index_artifact(org, "bot-r", _artifact(), meeting_date=1.0)
    with _admin(pg) as raw:
        cols = {
            r[0] for r in raw.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='meeting_memory'"
            ).fetchall()
        }
        assert "transcript" not in cols
        blob = "".join(str(r) for r in raw.execute(
            "SELECT search_text, summary FROM meeting_memory WHERE org_id=%s",
            (org,)).fetchall())
    assert "SECRETSAUCE-do-not-index" not in blob


def test_rls_cross_org_isolation(cp, pg):
    org_a = _org(cp, "mma")
    org_b = _org(cp, "mmb")
    meeting_memory.index_artifact(org_a, "bot-a", _artifact(), meeting_date=1.0)
    meeting_memory.index_artifact(
        org_b, "bot-b", _artifact(summary="Other tenant"), meeting_date=1.0)
    # API isolation: org A search never surfaces org B's meeting.
    a = meeting_memory.search(org_a, "ACME", principal_ref="u_runner")
    assert [r["meeting_id"] for r in a] == ["bot-a"]
    # RLS backstop: an org-B-pinned connection cannot read org A's rows.
    with psycopg.connect(pg["app_uri"], autocommit=True) as raw:
        raw.execute("SELECT set_config('app.current_org', %s, false)",
                    (org_b,))
        assert raw.execute(
            "SELECT count(*) FROM meeting_memory WHERE org_id=%s",
            (org_a,)).fetchone()[0] == 0
