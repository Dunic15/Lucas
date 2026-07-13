"""Production Postgres outbox invariants on the exact laura_app role.

The suite applies the complete Alembic chain with an admin DSN, then exercises
all runtime operations through laura_app (NOSUPERUSER/NOBYPASSRLS).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import control_plane, outbox, outbox_pg  # noqa: E402
from app.cedric import callback, integration  # noqa: E402
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
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("outbox_pg")))
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
    app_uri = ci.make_conninfo(
        dbname=info.get("dbname"),
        user=APP_ROLE,
        password="pw",
        host=info.get("host"),
        port=info.get("port"),
    )
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
    yield {
        "uri": uri,
        "app_uri": app_uri,
        "app_sa_url": app_sa_url,
    }
    control_plane.reset_engine()
    srv.cleanup()


def _admin(pg):
    return psycopg.connect(pg["uri"], autocommit=True)


@pytest.fixture
def cp(pg, monkeypatch):
    monkeypatch.setattr(settings, "laura_database_url", pg["app_sa_url"])
    control_plane.reset_engine()
    with _admin(pg) as conn:
        conn.execute("DELETE FROM callback_outbox")
        conn.execute("DELETE FROM queued_actions")
    yield control_plane
    control_plane.reset_engine()


def _org(cp, tag: str) -> str:
    stamp = str(time.time_ns())
    return cp.ensure_user(
        f"sub-{tag}-{stamp}",
        f"{tag}-{stamp}@freemail.test",
        tag,
        "",
    )["org_id"]


def _integration(org: str) -> dict:
    return {
        "org_id": org,
        "callback_url": "https://www.meet-cedric.com/api/laura/events",
        "external_ref": {
            "team": "T_TEST",
            "slack_channel": "C_TEST",
            "ignored_secret": "must-not-persist",
        },
    }


def _item(action_id: str) -> dict:
    return {
        "action_id": action_id,
        "action": "Send the reviewed rollout note",
        "owner": "Alex",
        "due": "Friday",
    }


def _enqueue(org: str, bot: str, key: str) -> int:
    value = outbox._enqueue(
        event="action.requested",
        idempotency_key=key,
        integration=_integration(org),
        bot_id=bot,
        action_id=key.rsplit(":", 1)[-1],
        payload={
            "event": "action.requested",
            "org_id": org,
            "bot_id": bot,
            "action_id": key.rsplit(":", 1)[-1],
            "action": "Ship it",
        },
    )
    assert value is not None
    return value


def test_runtime_role_grants_and_force_rls(cp, pg):
    org_a = _org(cp, "grant-a")
    org_b = _org(cp, "grant-b")
    _enqueue(org_a, "bot-a", "action.requested:a")
    _enqueue(org_b, "bot-b", "action.requested:b")

    with psycopg.connect(pg["app_uri"], autocommit=True) as conn:
        role = conn.execute(
            "SELECT current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert role == (APP_ROLE, False, False)
        conn.execute(
            "SELECT set_config('app.current_org', %s, false)", (org_a,)
        )
        visible = conn.execute(
            "SELECT org_id::text FROM callback_outbox ORDER BY id"
        ).fetchall()
        assert visible == [(org_a,)]
        changed = conn.execute(
            "UPDATE callback_outbox SET last_error='x' WHERE org_id=%s",
            (org_b,),
        )
        assert changed.rowcount == 0
        assert conn.execute(
            "SELECT has_table_privilege(current_user, "
            "'public.callback_outbox', 'DELETE')"
        ).fetchone()[0] is False
        assert conn.execute(
            "SELECT has_function_privilege(current_user, "
            "'laura_private.due_callback_orgs(integer)', 'EXECUTE')"
        ).fetchone()[0] is True

    with _admin(pg) as conn:
        forced = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid='public.callback_outbox'::regclass"
        ).fetchone()
        assert forced == (True, True)


def test_action_and_callback_commit_or_rollback_together(cp, pg, monkeypatch):
    org = _org(cp, "atomic")
    session = SimpleNamespace(
        org_id=org,
        bot_id="bot-atomic",
        integration=_integration(org),
    )
    item = _item("act-atomic")
    original = outbox_pg._callback_insert

    def explode(conn, callback_record):
        raise RuntimeError("simulated callback insert failure")

    monkeypatch.setattr(outbox_pg, "_callback_insert", explode)
    with pytest.raises(outbox.OutboxUnavailable):
        outbox.persist_action_capture(session, item)
    with _admin(pg) as conn:
        assert conn.execute(
            "SELECT count(*) FROM queued_actions WHERE org_id=%s", (org,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM callback_outbox WHERE org_id=%s", (org,)
        ).fetchone()[0] == 0

    monkeypatch.setattr(outbox_pg, "_callback_insert", original)
    outbox_id = outbox.persist_action_capture(session, item)
    assert outbox_id is not None
    with _admin(pg) as conn:
        counts = conn.execute(
            "SELECT "
            "(SELECT count(*) FROM queued_actions WHERE org_id=%s), "
            "(SELECT count(*) FROM callback_outbox WHERE org_id=%s)",
            (org, org),
        ).fetchone()
    assert counts == (1, 1)


def test_session_ended_payload_is_allowlisted_and_idempotent(cp, pg):
    org = _org(cp, "ended-safe")
    artifact = {
        "summary": "Reviewed summary",
        "actions": [],
        "decisions": [],
        "transcript": "RAW TRANSCRIPT MUST NEVER ENTER POSTGRES",
        "raw_transcript": "SECOND RAW FIELD",
        "utterances": [{"text": "secret spoken content"}],
        "meeting_url": "https://meet.google.com/private-room",
    }
    assert integration.deliver_ended(
        _integration(org), "bot-ended", artifact
    ) is True
    assert integration.deliver_ended(
        _integration(org), "bot-ended", artifact
    ) is True
    with _admin(pg) as conn:
        rows = conn.execute(
            "SELECT payload_json::text FROM callback_outbox "
            "WHERE org_id=%s AND event='session.ended'",
            (org,),
        ).fetchall()
    assert len(rows) == 1
    payload = rows[0][0]
    assert "Reviewed summary" in payload
    assert "RAW TRANSCRIPT" not in payload
    assert "SECOND RAW FIELD" not in payload
    assert "secret spoken content" not in payload
    assert "private-room" not in payload


def test_two_org_reads_claims_and_retry_are_isolated(cp):
    org_a = _org(cp, "iso-a")
    org_b = _org(cp, "iso-b")
    id_a = _enqueue(org_a, "bot-a", "action.requested:iso-a")
    id_b = _enqueue(org_b, "bot-b", "action.requested:iso-b")

    assert [row["id"] for row in outbox.delivery_rows(org_a)] == [id_a]
    assert [row["id"] for row in outbox.delivery_rows(org_b)] == [id_b]
    assert outbox.retry_status(org_a, id_b) == "missing"
    claimed = outbox_pg.claim_due(org_a, 10, now=time.time() + 1)
    assert [row["id"] for row in claimed] == [id_a]
    assert [row["id"] for row in outbox.delivery_rows(org_b)] == [id_b]


def test_restart_worker_discovers_tenants_without_local_sessions(
    cp, monkeypatch
):
    org_a = _org(cp, "restart-a")
    org_b = _org(cp, "restart-b")
    _enqueue(org_a, "bot-restart-a", "action.requested:restart-a")
    _enqueue(org_b, "bot-restart-b", "action.requested:restart-b")
    sent: list[str] = []

    def post(url, payload, *, idempotency_key=""):
        sent.append(idempotency_key)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(callback, "_post", post)
    # No org_id and no local Session objects: the runtime role discovers only
    # due tenant UUIDs through the private function, then claims payloads under
    # each tenant's FORCE-RLS context.
    assert outbox.process_due(limit=10, now=time.time() + 2) == 2
    assert set(sent) == {
        "action.requested:restart-a",
        "action.requested:restart-b",
    }
    assert outbox.delivery_rows(org_a)[0]["status"] == "delivered"
    assert outbox.delivery_rows(org_b)[0]["status"] == "delivered"


def test_multi_worker_skip_locked_and_lease_recovery(cp):
    org = _org(cp, "workers")
    ids = {
        _enqueue(org, "bot-1", "action.requested:worker-1"),
        _enqueue(org, "bot-2", "action.requested:worker-2"),
    }
    now = time.time() + 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _n: outbox_pg.claim_due(org, 1, now=now),
                range(2),
            )
        )
    claimed = [row for batch in results for row in batch]
    assert {row["id"] for row in claimed} == ids
    assert len({str(row["lease_token"]) for row in claimed}) == 2

    first = claimed[0]
    assert outbox_pg.claim_due(
        org, 1, outbox_id=first["id"], now=now + 59
    ) == []
    recovered = outbox_pg.claim_due(
        org, 1, outbox_id=first["id"], now=now + 61
    )
    assert len(recovered) == 1
    assert recovered[0]["id"] == first["id"]
    assert str(recovered[0]["lease_token"]) != str(first["lease_token"])


def test_transient_failure_retries_same_idempotency_key(cp, monkeypatch):
    org = _org(cp, "retry")
    outbox_id = _enqueue(
        org, "bot-retry", "action.requested:retry-same-key"
    )
    calls: list[str] = []

    def post(url, payload, *, idempotency_key=""):
        calls.append(idempotency_key)
        if len(calls) == 1:
            raise TimeoutError("before response")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(callback, "_post", post)
    now = time.time() + 2
    assert outbox.process_due(limit=1, now=now, org_id=org) == 0
    failed = outbox.delivery_rows(org)[0]
    assert failed["id"] == outbox_id
    assert failed["status"] == "failed"
    assert failed["attempts"] == 1
    assert outbox.process_due(limit=1, now=now + 2, org_id=org) == 1
    delivered = outbox.delivery_rows(org)[0]
    assert delivered["status"] == "delivered"
    assert delivered["attempts"] == 2
    assert calls == [
        "action.requested:retry-same-key",
        "action.requested:retry-same-key",
    ]


def test_crash_after_send_recovers_expired_lease_with_same_key(
    cp, monkeypatch
):
    org = _org(cp, "after-send")
    _enqueue(org, "bot-after", "action.requested:after-send")
    calls: list[str] = []
    original_finish = outbox_pg.finish_attempt
    first = True

    def post(url, payload, *, idempotency_key=""):
        calls.append(idempotency_key)
        return SimpleNamespace(status_code=200)

    def crash_once(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            raise RuntimeError("crash after peer accepted")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(callback, "_post", post)
    monkeypatch.setattr(outbox_pg, "finish_attempt", crash_once)
    now = time.time() + 2
    with pytest.raises(outbox.OutboxUnavailable):
        outbox.process_due(limit=1, now=now, org_id=org)
    row = outbox.delivery_rows(org)[0]
    assert row["status"] == "sending"
    assert row["attempts"] == 0

    monkeypatch.setattr(outbox_pg, "finish_attempt", original_finish)
    assert outbox.process_due(limit=1, now=now + 61, org_id=org) == 1
    assert outbox.delivery_rows(org)[0]["status"] == "delivered"
    assert calls == [
        "action.requested:after-send",
        "action.requested:after-send",
    ]


def test_manual_retry_does_not_steal_live_lease(cp, pg):
    org = _org(cp, "manual-busy")
    outbox_id = _enqueue(
        org, "bot-busy", "action.requested:manual-busy"
    )
    outbox_pg.claim_due(org, 1, outbox_id=outbox_id)
    assert outbox.retry_status(org, outbox_id) == "busy"
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE callback_outbox SET lease_until=clock_timestamp()-"
            "interval '1 second' WHERE id=%s",
            (outbox_id,),
        )
    assert outbox.retry_status(org, outbox_id) == "queued"
