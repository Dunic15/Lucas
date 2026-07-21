"""Canonical Action plane (M0) invariants on real Postgres as laura_app.

Applies the full Alembic chain (through 0009_canonical_actions) with an admin
DSN, then proves through the policy-bound runtime role:

  1. UNIQUE(org_id, idempotency_key) is enforced on queued_actions;
  2. the execution claim (approved → executing CAS) has exactly ONE winner
     under real concurrency; the double-approval acceptance gate;
  3. the durable decision record is first-write-wins under concurrency;
  4. FORCE RLS isolates action_decisions and claims across orgs;
  5. session-ended indexing stamps typed/schema/risk/route/origin_avatar and
     needs_details; the params door edit moves it to proposed with a log;
  6. receipts persist and the status rank guard holds for the new states.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import control_plane, outbox_pg  # noqa: E402
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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("canon_pg")))
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
def cp(pg, monkeypatch):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    control_plane.reset_engine()
    with _admin(pg) as conn:
        conn.execute("DELETE FROM action_decisions")
        conn.execute("DELETE FROM callback_outbox")
        conn.execute("DELETE FROM action_capture_events")
        conn.execute("DELETE FROM action_finalize_state")
        conn.execute("DELETE FROM queued_actions")
    yield control_plane
    control_plane.reset_engine()


def _org(cp, tag: str) -> str:
    stamp = str(time.time_ns())
    return cp.ensure_user(
        f"sub-{tag}-{stamp}", f"{tag}-{stamp}@freemail.test", tag, ""
    )["org_id"]


_TYPED_MISSING = {"type": "email.send",
                  "args": {"to": [], "subject": "Recap", "body": "Notes"}}


def _seed(org: str, action_id: str, typed: dict | None = _TYPED_MISSING) -> None:
    action = {"action_id": action_id, "item": "Email the recap", "owner": "Ben"}
    if typed is not None:
        action["typed"] = typed
        action["execution_route"] = "native"
    outbox_pg.index_session_ended_actions(
        org, f"bot-{action_id}",
        {"avatar_id": "laura", "actions": [action]},
    )


# ── 1. enforced idempotency key ──

def test_idempotency_key_unique_per_org(cp, pg):
    org = _org(cp, "idem")
    _seed(org, "i1", typed=None)
    _seed(org, "i2", typed=None)
    assert outbox_pg.claim_action_execution(
        org, "i1", idempotency_key="exec:shared"
    ) == "claimed"
    with _admin(pg) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "UPDATE queued_actions SET idempotency_key='exec:shared' "
                "WHERE org_id=%s AND action_id='i2'",
                (org,),
            )


# ── 2. the execution claim race ──

def test_claim_race_has_exactly_one_winner(cp):
    org = _org(cp, "race")
    _seed(org, "r1", typed=None)

    def claim(via: str) -> str:
        return outbox_pg.claim_action_execution(
            org, "r1", idempotency_key="exec:r1", detail=f"via {via}"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(claim, ["dashboard", "slack"]))
    assert results == ["claimed", "lost"]

    # Terminal settles the claim; a later claim attempt still loses.
    assert outbox_pg.set_action_status(
        org, "r1", "done", "native · email · m-1",
        {"kind": "email", "ref": "m-1", "route": "native"},
    )
    assert outbox_pg.claim_action_execution(org, "r1") == "lost"
    # An action that was never indexed is 'missing': callers fall back.
    assert outbox_pg.claim_action_execution(org, "ghost") == "missing"


# ── 3. durable decision record ──

def test_decision_record_first_write_wins(cp):
    org = _org(cp, "dec")
    _seed(org, "d1", typed=None)

    def record(via: str) -> bool:
        return outbox_pg.record_action_decision(org, "d1", {
            "decision": "approve", "decided_via": via,
            "previous_status": "proposed", "new_status": "approved",
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(record, ["dashboard", "slack"]))
    assert results == [False, True]

    recorded = outbox_pg.get_action_decision(org, "d1")
    assert recorded is not None and recorded["decision"] == "approve"

    outbox_pg.set_action_decision_result(org, "d1", "done", "job-1")
    recorded = outbox_pg.get_action_decision(org, "d1")
    assert recorded["new_status"] == "done"
    assert recorded["execution_job_id"] == "job-1"


# ── 4. FORCE RLS isolation ──

def test_rls_isolates_decisions_and_claims(cp, pg):
    org_a = _org(cp, "rls-a")
    org_b = _org(cp, "rls-b")
    _seed(org_a, "x1", typed=None)
    assert outbox_pg.record_action_decision(org_a, "x1", {
        "decision": "approve", "decided_via": "dashboard",
    })

    # Org B sees neither the decision nor the action.
    assert outbox_pg.get_action_decision(org_b, "x1") is None
    assert outbox_pg.get_action(org_b, "x1") is None
    assert outbox_pg.claim_action_execution(org_b, "x1") == "missing"

    # Raw role-level check: with org B's GUC the row is invisible.
    import psycopg.conninfo as ci

    info = ci.conninfo_to_dict(pg["uri"])
    app_uri = ci.make_conninfo(
        dbname=info.get("dbname"), user=APP_ROLE, password="pw",
        host=info.get("host"), port=info.get("port"),
    )
    with psycopg.connect(app_uri, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.current_org', %s, false)", (org_b,))
        rows = conn.execute("SELECT action_id FROM action_decisions").fetchall()
        assert rows == []
        denied = conn.execute(
            "UPDATE action_decisions SET new_status='done' WHERE org_id=%s",
            (org_a,),
        )
        assert denied.rowcount == 0


# ── 5. needs_details stamping + params edit ──

def test_index_stamps_canonical_fields_and_needs_details(cp):
    org = _org(cp, "stamp")
    _seed(org, "n1")  # typed email.send with empty required 'to'

    row = outbox_pg.get_action(org, "n1")
    assert row is not None
    assert row["execution_status"] == "needs_details"
    assert row["typed_json"]["type"] == "email.send"
    assert any(f["name"] == "to" for f in row["params_schema_json"])
    assert row["risk"] == "medium"
    assert row["execution_route"] == "native"
    assert row["origin_avatar"] == "laura"

    typed = outbox_pg.update_action_params(org, "n1", {"to": ["a@b.co"]})
    assert typed is not None and typed["args"]["to"] == ["a@b.co"]
    row = outbox_pg.get_action(org, "n1")
    assert row["execution_status"] == "proposed"
    assert any(e.get("event") == "params_edited" for e in row["logs_json"])

    # Edits are refused once execution starts / finishes.
    assert outbox_pg.claim_action_execution(org, "n1") == "claimed"
    assert outbox_pg.update_action_params(org, "n1", {"subject": "x"}) is None


# ── 6. receipts + rank guard with the new states ──

def test_receipt_persists_and_rank_guard_holds(cp):
    org = _org(cp, "rank")
    _seed(org, "k1", typed=None)

    assert outbox_pg.set_action_status(org, "k1", "approved", "ok")
    assert outbox_pg.claim_action_execution(org, "k1") == "claimed"
    assert outbox_pg.set_action_status(
        org, "k1", "done", "native · email · m-9",
        {"kind": "email", "ref": "m-9", "route": "native"},
    )
    row = outbox_pg.get_action(org, "k1")
    assert row["receipt_json"] == {"kind": "email", "ref": "m-9",
                                   "route": "native"}
    assert row["execution_lease_until"] == 0  # cleared on terminal

    # Late, out-of-order 'proposed' report cannot repaint the terminal state.
    assert outbox_pg.set_action_status(org, "k1", "proposed", "late replay")
    assert outbox_pg.get_action(org, "k1")["execution_status"] == "done"

    # 'executing' outranks a late 'approved' repaint attempt.
    _seed(org, "k2", typed=None)
    assert outbox_pg.claim_action_execution(org, "k2") == "claimed"
    assert outbox_pg.set_action_status(org, "k2", "approved", "late")
    assert outbox_pg.get_action(org, "k2")["execution_status"] == "executing"
