"""M0/M1 hardening on real Postgres: stale-executing reconcile + index epochs.

Two prerequisites gating rollout (owner directives, 2026-07-17):

1. ACTION_DISPATCH_ASYNC must not be enabled until a crashed 'executing'
   claim settles honestly: calendar actions are verified by READING the
   calendar (found → done with a real receipt; absent → failed 'not
   created'); unverifiable types settle failed with an explicit
   execution_unknown receipt only after a grace period. Never a blind retry.

2. COMPANY_BRAIN_ENABLED must not go to production until instances that do
   NOT share a disk converge their per-org index files: a monotonic epoch
   (max rebuild-job id) + per-instance sidecar markers, refreshed by every
   instance's worker loop.
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

from app import action_reconcile, avatars, control_plane, rag, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.knowledge import dal, ingest  # noqa: E402
from app.knowledge import router as knowledge_router  # noqa: E402
from app import outbox_pg  # noqa: E402

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
            "comment = 'test shim: gen_random_uuid() is core since PG13'\n"
        ),
        "pgcrypto--1.0.sql": "-- shim: no objects; gen_random_uuid() is core\n",
        "citext.control": (
            "default_version = '1.0'\nrelocatable = true\n"
            "comment = 'test shim: citext as a plain-text domain'\n"
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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("hardening_pg")))
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
    query = {
        key: str(info[key])
        for key in ("host", "port")
        if info.get(key) is not None
    }
    app_sa_url = URL.create(
        "postgresql+psycopg",
        username=APP_ROLE,
        password="pw",
        database=info.get("dbname"),
        query=query,
    ).render_as_string(hide_password=False)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env={
            **os.environ,
            "LAURA_DATABASE_ADMIN_URL": admin_sa_url,
            "LAURA_DATABASE_URL": "",
            "LAURA_REQUIRE_MIGRATIONS": "1",
        },
        capture_output=True,
        text=True,
        timeout=600,
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
    monkeypatch.setattr(settings, "company_brain_enabled", True)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "a" / "store.sqlite3")
    (tmp_path / "a").mkdir(exist_ok=True)
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    action_reconcile._reset_for_tests()
    ingest._last_refresh = 0.0
    control_plane.reset_engine()
    with _admin(pg) as conn:
        for table in ("knowledge_sync_jobs", "knowledge_chunks",
                      "knowledge_document_versions", "knowledge_documents",
                      "knowledge_assignments", "knowledge_sources",
                      "action_decisions", "callback_outbox",
                      "action_capture_events", "action_finalize_state",
                      "queued_actions"):
            conn.execute(f"DELETE FROM {table}")
    yield control_plane
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    action_reconcile._reset_for_tests()
    control_plane.reset_engine()


def _org(cp, tag: str) -> str:
    stamp = str(time.time_ns())
    return cp.ensure_user(
        f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test", tag, ""
    )["org_id"]


_CAL_TYPED = {
    "type": "calendar.create_event",
    "args": {"title": "Quarterly sync", "start": "2026-08-03T10:00:00",
             "end": "2026-08-03T10:30:00"},
}
_EMAIL_TYPED = {
    "type": "email.send",
    "args": {"to": ["x@y.co"], "subject": "s", "body": "b"},
}


def _seed_executing(pg, org: str, action_id: str, typed: dict,
                    expired_seconds: float) -> None:
    outbox_pg.index_session_ended_actions(
        org, f"bot-{action_id}",
        {"avatar_id": "laura", "actions": [
            {"action_id": action_id, "item": "do the thing", "typed": typed,
             "execution_route": "native"},
        ]},
    )
    assert outbox_pg.claim_action_execution(org, action_id) == "claimed"
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE queued_actions SET execution_lease_until = "
            "clock_timestamp() - (%s * interval '1 second') "
            "WHERE org_id=%s AND action_id=%s",
            (expired_seconds, org, action_id),
        )


# ── 1. stale-executing reconciler ───────────────────────────────────────────

def test_calendar_claim_verified_found_settles_done(cp, pg, monkeypatch):
    org = _org(cp, "rec-found")
    _seed_executing(pg, org, "c1", _CAL_TYPED, expired_seconds=10)

    from app import google_client

    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org_id, **kw: {"ok": True, "events": [
            {"summary": "Quarterly  sync", "htmlLink": "https://cal/ev-99"},
        ]},
    )
    assert action_reconcile.maybe_reconcile(org) == 1
    row = outbox_pg.get_action(org, "c1")
    assert row["execution_status"] == "done"
    assert row["receipt_json"]["ref"] == "https://cal/ev-99"
    assert row["receipt_json"]["reconciled"] is True


def test_calendar_claim_verified_absent_settles_failed(cp, pg, monkeypatch):
    org = _org(cp, "rec-absent")
    _seed_executing(pg, org, "c2", _CAL_TYPED, expired_seconds=10)

    from app import google_client

    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org_id, **kw: {"ok": True, "events": [
            {"summary": "some other meeting", "htmlLink": "https://cal/other"},
        ]},
    )
    assert action_reconcile.maybe_reconcile(org) == 1
    row = outbox_pg.get_action(org, "c2")
    assert row["execution_status"] == "failed"
    assert "NOT created" in row["execution_detail"]


def test_unverifiable_claim_waits_for_grace_then_settles(cp, pg, monkeypatch):
    org = _org(cp, "rec-grace")
    _seed_executing(pg, org, "e1", _EMAIL_TYPED, expired_seconds=10)

    # Inside the grace window: nothing settles, the claim stays observable.
    assert action_reconcile.maybe_reconcile(org) == 0
    assert outbox_pg.get_action(org, "e1")["execution_status"] == "executing"

    # Beyond the grace window: settles failed with the explicit
    # execution_unknown receipt (never a retry).
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE queued_actions SET execution_lease_until = "
            "clock_timestamp() - interval '1000 seconds' "
            "WHERE org_id=%s AND action_id=%s",
            (org, "e1"),
        )
    action_reconcile._reset_for_tests()  # bypass the per-org throttle
    assert action_reconcile.maybe_reconcile(org) == 1
    row = outbox_pg.get_action(org, "e1")
    assert row["execution_status"] == "failed"
    assert "execution_unknown" in row["execution_detail"]
    assert row["receipt_json"]["kind"] == "unknown"


def test_reconcile_is_throttled_per_org(cp, pg, monkeypatch):
    org = _org(cp, "rec-throttle")
    _seed_executing(pg, org, "t1", _EMAIL_TYPED, expired_seconds=2000)

    calls = {"n": 0}
    real = outbox_pg.stale_executing

    def counting(org_id):
        calls["n"] += 1
        return real(org_id)

    monkeypatch.setattr(outbox_pg, "stale_executing", counting)
    assert action_reconcile.maybe_reconcile(org) == 1
    # Second pass inside the TTL never even scans.
    assert action_reconcile.maybe_reconcile(org) == 0
    assert calls["n"] == 1


# ── 2. multi-instance index convergence ─────────────────────────────────────

def _drain_jobs(rounds: int = 6) -> None:
    for _ in range(rounds):
        if ingest.process_due() == 0:
            return


def _as_instance(monkeypatch, tmp_path, name: str) -> None:
    """Point the process at a different instance-local disk."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(store, "STORE_PATH", root / "store.sqlite3")
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    ingest._last_refresh = 0.0


def test_index_files_converge_across_instances(cp, pg, monkeypatch, tmp_path):
    org = _org(cp, "multi")
    avatar = avatars.load("laura")

    # ── instance A ingests ──
    _as_instance(monkeypatch, tmp_path, "a")
    src = dal.create_source(org, "Handbook", "upload")
    code, payload = knowledge_router._upload_document(
        org, src["id"], {"filename": "handbook.md",
                         "text": "# Refunds\n\nRefunds take THREE days."}
    )
    assert code == 200, payload
    assert dal.assign(org, src["id"], "laura")
    _drain_jobs()
    text_a = " ".join(
        h.text for h in rag.retrieve(avatar, "refunds", k=4, org_id=org)
    )
    assert "THREE days" in text_a

    # ── instance B has a different (empty) disk: stale until refresh ──
    _as_instance(monkeypatch, tmp_path, "b")
    text_b = " ".join(
        h.text for h in rag.retrieve(avatar, "refunds", k=4, org_id=org)
    )
    assert "THREE days" not in text_b  # nothing local yet
    assert ingest.refresh_local_indexes() >= 1  # epoch check pulls it in
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    text_b = " ".join(
        h.text for h in rag.retrieve(avatar, "refunds", k=4, org_id=org)
    )
    assert "THREE days" in text_b

    # A repeated refresh with an unchanged epoch does nothing.
    ingest._last_refresh = 0.0
    assert ingest.refresh_local_indexes() == 0

    # ── delete on instance B; instance A must converge to gone ──
    assert dal.delete_source(org, src["id"])
    _drain_jobs()  # B claims the rebuild job, updates ITS files+marker
    _as_instance(monkeypatch, tmp_path, "a")
    stale_a = " ".join(
        h.text for h in rag.retrieve(avatar, "refunds", k=4, org_id=org)
    )
    assert "THREE days" in stale_a  # A is provably stale before the refresh
    assert ingest.refresh_local_indexes() >= 1
    rag._ORG_CACHE.clear()
    rag._ORG_MISS.clear()
    after_a = " ".join(
        h.text for h in rag.retrieve(avatar, "refunds", k=4, org_id=org)
    )
    assert "THREE days" not in after_a
