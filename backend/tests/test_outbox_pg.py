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

from app import control_plane, ledger, outbox, outbox_pg, store, tools  # noqa: E402
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
        conn.execute("DELETE FROM action_capture_events")
        conn.execute("DELETE FROM action_finalize_state")
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


def _due_after_settle(offset: float = 1.0) -> float:
    """Clock safely beyond the intentional live-action settle fence."""
    return time.time() + outbox._ACTION_SETTLE_SECONDS + offset


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


def _local_caps_store(tmp_path, monkeypatch, name: str) -> None:
    """Point the avatar-keyed capability switch at a fresh local SQLite file.

    Capabilities are read from the local store regardless of the Postgres
    control plane (avatar-keyed, split-brain safe), so a PG test that flips a
    switch must give store._connect() a table to read.
    """
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / name)
    store._init_db()


def test_session_ended_slack_off_indexes_actions_without_callback(
    cp, pg, tmp_path, monkeypatch
):
    """slack=OFF suppresses the Cedric fan-out but NOT the native action list:
    queued_actions is fully indexed (incl. the artifact-only summarizer action)
    and NO session.ended callback_outbox row is written."""
    _local_caps_store(tmp_path, monkeypatch, "caps-off.sqlite3")
    store.set_avatar_capability("laura", "slack", False)
    org = _org(cp, "ended-slackoff")
    artifact = {
        "avatar_id": "laura",
        "summary": "Reviewed summary",
        "actions": [
            {"action_id": "live-1", "item": "Send the recap", "owner": "Ben"},
            {"action_id": "summ-1", "item": "Book the follow-up", "owner": ""},
        ],
    }
    # No deliverable callback row is enqueued for a slack-off avatar.
    assert outbox.enqueue_session_ended(
        _integration(org), "bot-slackoff", artifact
    ) is None
    with _admin(pg) as conn:
        callbacks = conn.execute(
            "SELECT count(*) FROM callback_outbox "
            "WHERE org_id=%s AND event='session.ended'",
            (org,),
        ).fetchone()[0]
        actions = conn.execute(
            "SELECT action_id FROM queued_actions WHERE org_id=%s "
            "ORDER BY action_id",
            (org,),
        ).fetchall()
    assert callbacks == 0  # NO Slack fan-out to Cedric
    assert actions == [("live-1",), ("summ-1",)]  # native list fully indexed


def test_session_ended_slack_off_indexing_is_retry_idempotent(
    cp, pg, tmp_path, monkeypatch
):
    """A finalize retry re-mints the summarizer-only action under a fresh id;
    with no callback row as the anchor, text-dedup still prevents a phantom."""
    _local_caps_store(tmp_path, monkeypatch, "caps-retry.sqlite3")
    store.set_avatar_capability("laura", "slack", False)
    org = _org(cp, "ended-slackoff-retry")
    first = {
        "avatar_id": "laura", "summary": "s",
        "actions": [{"action_id": "summ-A", "item": "Book the follow-up"}],
    }
    retry = {
        "avatar_id": "laura", "summary": "s",
        "actions": [{"action_id": "summ-B", "item": "Book the follow-up"}],
    }
    assert outbox.enqueue_session_ended(
        _integration(org), "bot-retry", first
    ) is None
    assert outbox.enqueue_session_ended(
        _integration(org), "bot-retry", retry
    ) is None
    with _admin(pg) as conn:
        actions = conn.execute(
            "SELECT action_id FROM queued_actions WHERE org_id=%s", (org,)
        ).fetchall()
    assert actions == [("summ-A",)]  # summ-B deduped by text — no phantom row


def test_two_org_reads_claims_and_retry_are_isolated(cp):
    org_a = _org(cp, "iso-a")
    org_b = _org(cp, "iso-b")
    id_a = _enqueue(org_a, "bot-a", "action.requested:iso-a")
    id_b = _enqueue(org_b, "bot-b", "action.requested:iso-b")

    assert [row["id"] for row in outbox.delivery_rows(org_a)] == [id_a]
    assert [row["id"] for row in outbox.delivery_rows(org_b)] == [id_b]
    assert outbox.retry_status(org_a, id_b) == "missing"
    claimed = outbox_pg.claim_due(org_a, 10, now=_due_after_settle())
    assert [row["id"] for row in claimed] == [id_a]
    assert [row["id"] for row in outbox.delivery_rows(org_b)] == [id_b]


def test_restart_worker_discovers_tenants_without_local_sessions(
    cp, pg, monkeypatch
):
    org_a = _org(cp, "restart-a")
    org_b = _org(cp, "restart-b")
    _enqueue(org_a, "bot-restart-a", "action.requested:restart-a")
    _enqueue(org_b, "bot-restart-b", "action.requested:restart-b")
    # Global tenant discovery intentionally uses the database clock rather
    # than the worker's injected test clock. Backdate the real rows so the
    # discovery query sees them as due without weakening production semantics.
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE callback_outbox "
            "SET next_attempt_at=clock_timestamp()-interval '1 second' "
            "WHERE org_id IN (%s, %s)",
            (org_a, org_b),
        )
    sent: list[str] = []

    def post(url, payload, *, idempotency_key=""):
        sent.append(idempotency_key)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(callback, "_post", post)
    # No org_id and no local Session objects: the runtime role discovers only
    # due tenant UUIDs through the private function, then claims payloads under
    # each tenant's FORCE-RLS context.
    assert outbox.process_due(limit=10, now=_due_after_settle(2)) == 2
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
    now = _due_after_settle(2)
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
    now = _due_after_settle(2)
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
    now = _due_after_settle(2)
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
    outbox_pg.claim_due(
        org, 1, outbox_id=outbox_id, now=_due_after_settle()
    )
    assert outbox.retry_status(org, outbox_id) == "busy"
    with _admin(pg) as conn:
        conn.execute(
            "UPDATE callback_outbox SET lease_until=clock_timestamp()-"
            "interval '1 second' WHERE id=%s",
            (outbox_id,),
        )
    assert outbox.retry_status(org, outbox_id) == "queued"



def test_duplicate_recall_final_concurrent_and_restart_is_one_capture(cp, pg):
    org = _org(cp, "producer-replay")
    integration_data = _integration(org)

    def session():
        return SimpleNamespace(
            org_id=org,
            bot_id="bot-producer-replay",
            integration=integration_data,
            queued_actions=[],
        )

    def capture(_n):
        return tools.capture_action_once(
            session(),
            "Send the approved recap",
            source_event_key="a" * 64,
            source_fingerprint="b" * 64,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(capture, range(4)))
    assert sum(1 for _item_value, created in results if created) == 1
    assert len({item_value["action_id"] for item_value, _ in results}) == 1

    # Process restart: no Session memory survives, but the same final still
    # resolves to the original action and stable Slack idempotency key.
    replay_item, replay_created = capture(99)
    assert replay_created is False
    action_id = replay_item["action_id"]

    with _admin(pg) as conn:
        actions = conn.execute(
            "SELECT action_id FROM queued_actions WHERE org_id=%s", (org,)
        ).fetchall()
        callbacks = conn.execute(
            "SELECT idempotency_key FROM callback_outbox WHERE org_id=%s",
            (org,),
        ).fetchall()
    assert actions == [(action_id,)]
    assert callbacks == [(f"action.requested:{action_id}",)]

    # Same words at a genuinely later Recall interval have another exact key
    # and therefore represent a new requested action, not a retry.
    later, later_created = tools.capture_action_once(
        session(),
        "Send the approved recap",
        source_event_key="c" * 64,
        source_fingerprint="b" * 64,
    )
    assert later_created is True
    assert later["action_id"] != action_id



def test_continuation_is_append_once_and_updates_wire_before_claim(cp, pg):
    org = _org(cp, "continuation")
    session = SimpleNamespace(
        org_id=org,
        bot_id="bot-continuation",
        integration=_integration(org),
        queued_actions=[],
    )
    item, created = tools.capture_action_once(
        session,
        "Send the recap",
        source_event_key="initial-" + "a" * 56,
        source_fingerprint="initial-" + "b" * 56,
    )
    assert created is True

    # The card cannot leave during the four-second ASR continuation window.
    assert outbox_pg.claim_due(org, 10, now=time.time() + 1) == []

    def extend(_n):
        return tools.extend_action_once(
            session,
            item,
            "by Friday",
            source_event_key="continuation-" + "c" * 51,
            source_fingerprint="continuation-" + "d" * 51,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(extend, range(2)))
    assert sum(1 for _item_value, applied in results if applied) == 1

    claimed = outbox_pg.claim_due(org, 10, now=time.time() + 10)
    assert len(claimed) == 1
    payload = claimed[0]["payload_json"]
    assert payload["action"] == "Send the recap by Friday"
    assert claimed[0]["idempotency_key"] == (
        f"action.requested:{item['action_id']}"
    )
    with _admin(pg) as conn:
        assert conn.execute(
            "SELECT count(*) FROM action_capture_events WHERE org_id=%s",
            (org,),
        ).fetchone()[0] == 1



def test_finalize_fence_drains_inflight_capture_and_rejects_late(
    cp, pg, monkeypatch
):
    from threading import Event

    org = _org(cp, "finalize-fence")
    session = SimpleNamespace(
        org_id=org,
        bot_id="bot-finalize-fence",
        integration=_integration(org),
        queued_actions=[],
    )
    entered = Event()
    release = Event()
    original_insert = outbox_pg._callback_insert

    def blocked_insert(conn, callback_record):
        entered.set()
        assert release.wait(10)
        return original_insert(conn, callback_record)

    monkeypatch.setattr(outbox_pg, "_callback_insert", blocked_insert)
    with ThreadPoolExecutor(max_workers=2) as pool:
        capture_future = pool.submit(
            tools.capture_action_once,
            session,
            "Send the fenced recap",
            source_event_key="fence-inflight",
        )
        assert entered.wait(10)
        finalize_future = pool.submit(
            outbox.begin_action_finalize, org, session.bot_id
        )
        time.sleep(0.1)
        assert finalize_future.done() is False
        release.set()
        captured, created = capture_future.result(timeout=10)
        snapshot = finalize_future.result(timeout=10)

    assert created is True
    assert [row["action_id"] for row in snapshot] == [captured["action_id"]]
    with pytest.raises(outbox.ActionCaptureClosed):
        tools.capture_action_once(
            session,
            "This must not become a live-only card",
            source_event_key="fence-late",
        )
    with _admin(pg) as conn:
        assert conn.execute(
            "SELECT count(*) FROM queued_actions WHERE org_id=%s", (org,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM callback_outbox WHERE org_id=%s", (org,)
        ).fetchone()[0] == 1



def _fresh_local_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "ledger-instance.sqlite3")
    ledger._init_db()


def test_status_on_instance_b_closes_when_instance_a_finalizes(
    cp, tmp_path, monkeypatch
):
    org = _org(cp, "roundtrip-status")
    action_id = "action-cross-instance"
    artifact = {
        "summary": "Done",
        "actions": [{
            "action_id": action_id,
            "item": "Send the final rollout note",
            "owner": "Alex",
            "deadline": "Friday",
        }],
    }
    assert outbox.enqueue_session_ended(
        _integration(org), "bot-cross-instance", artifact
    ) is not None

    control_plane.reset_engine()  # instance B
    assert ledger.set_action_status(
        action_id, "done", "Slack execution complete", org_id=org
    )

    control_plane.reset_engine()  # instance A finalizes later
    _fresh_local_ledger(tmp_path, monkeypatch)
    ledger.record_meeting(
        "https://meet.google.com/abc-defg-hij",
        "laura",
        "bot-cross-instance",
        artifact,
        org_id=org,
    )
    rows = ledger.items("abc-defg-hij", org_id=org)
    assert len(rows) == 1
    assert rows[0]["action_id"] == action_id
    assert rows[0]["status"] == "done"
    assert rows[0]["resolution_detail"] == "Slack execution complete"

    control_plane.reset_engine()  # dashboard process
    state = ledger.action_statuses([action_id], org_id=org)[action_id]
    assert state["status"] == "done"
    assert state["detail"] == "Slack execution complete"
    assert state["updated_at"] > 0


def test_resolve_survives_restart_without_local_ledger(
    cp, tmp_path, monkeypatch
):
    org = _org(cp, "roundtrip-resolve")
    action_id = "action-after-restart"
    assert outbox.enqueue_session_ended(
        _integration(org),
        "bot-after-restart",
        {"actions": [
            {"action_id": action_id, "item": "Close the release ticket"}
        ]},
    ) is not None

    _fresh_local_ledger(tmp_path, monkeypatch)
    control_plane.reset_engine()
    assert ledger.resolve_by_action_id(
        action_id,
        outcome="rejected",
        detail="Owner declined in Slack",
        org_id=org,
    )
    control_plane.reset_engine()
    state = ledger.action_statuses([action_id], org_id=org)[action_id]
    assert state["status"] == "rejected"
    assert state["detail"] == "Owner declined in Slack"
    assert ledger.resolve_by_action_id(
        action_id, outcome="done", org_id=org
    ) is False


def test_action_index_status_and_resolve_are_force_rls_isolated(cp, pg):
    org_a = _org(cp, "action-rls-a")
    org_b = _org(cp, "action-rls-b")
    action_id = "same-visible-ref"
    assert outbox.enqueue_session_ended(
        _integration(org_a),
        "bot-action-a",
        {"actions": [{"action_id": action_id, "item": "A-only action"}]},
    ) is not None

    assert outbox_pg.set_action_status(org_b, action_id, "done") is False
    assert outbox_pg.resolve_action(org_b, action_id, "done") is False
    assert outbox_pg.action_statuses(org_b, [action_id]) == {}

    with psycopg.connect(pg["app_uri"], autocommit=True) as conn:
        conn.execute(
            "SELECT set_config('app.current_org', %s, false)", (org_b,)
        )
        changed = conn.execute(
            "UPDATE queued_actions SET execution_status='done' "
            "WHERE org_id=%s AND action_id=%s",
            (org_a, action_id),
        )
        assert changed.rowcount == 0

    assert outbox_pg.set_action_status(
        org_a, action_id, "approved", "approved by owner"
    )
    assert outbox_pg.action_statuses(org_a, [action_id])[action_id][
        "status"
    ] == "approved"



def test_pg_action_mutation_survives_broken_sqlite_mirror_and_retry(
    cp, monkeypatch
):
    org = _org(cp, "mirror-failure")
    status_id = "action-status-no-disk"
    resolve_id = "action-resolve-no-disk"
    assert outbox.enqueue_session_ended(
        _integration(org),
        "bot-mirror-failure",
        {"actions": [
            {"action_id": status_id, "item": "Report completion"},
            {"action_id": resolve_id, "item": "Close approval"},
        ]},
    ) is not None

    def broken_local_store():
        raise OSError("read-only local disk")

    monkeypatch.setattr(store, "_connect", broken_local_store)

    # Both requests commit in PG and still answer success when the optional
    # per-instance SQLite cache is unavailable.
    assert ledger.set_action_status(
        status_id, "done", "completed remotely", org_id=org
    )
    assert ledger.resolve_by_action_id(
        resolve_id, outcome="rejected", detail="declined remotely", org_id=org
    )

    # Lost HTTP responses are safe: same terminal retry remains successful.
    assert ledger.set_action_status(
        status_id, "done", "completed remotely", org_id=org
    )
    assert ledger.resolve_by_action_id(
        resolve_id, outcome="rejected", detail="declined remotely", org_id=org
    )
    states = outbox_pg.action_statuses(org, [status_id, resolve_id])
    assert states[status_id]["status"] == "done"
    assert states[status_id]["detail"] == "completed remotely"
    assert states[status_id]["updated_at"] > 0
    assert states[resolve_id]["status"] == "rejected"
    assert states[resolve_id]["detail"] == "declined remotely"
    assert states[resolve_id]["updated_at"] > 0


def test_pg_execution_state_is_monotonic_under_out_of_order_events(cp):
    org = _org(cp, "status-order")
    action_id = "action-out-of-order"
    assert outbox.enqueue_session_ended(
        _integration(org),
        "bot-status-order",
        {"actions": [{"action_id": action_id, "item": "Ship release"}]},
    ) is not None

    assert outbox_pg.set_action_status(
        org, action_id, "approved", "owner approved"
    )
    assert outbox_pg.set_action_status(
        org, action_id, "proposed", "late card-created replay"
    )
    state = outbox_pg.action_statuses(org, [action_id])[action_id]
    assert state["status"] == "approved"
    assert state["detail"] == "owner approved"

    assert outbox_pg.set_action_status(org, action_id, "done", "executed")
    assert outbox_pg.set_action_status(
        org, action_id, "approved", "late approval replay"
    )
    state = outbox_pg.action_statuses(org, [action_id])[action_id]
    assert state["status"] == "done"
    assert state["detail"] == "executed"


def test_durable_artifact_survives_engine_reset_with_retention(
    cp, pg, monkeypatch
):
    org = _org(cp, "artifact-restart")
    saved_at = 1_700_000_000.0
    artifact = {
        "org_id": org,
        "avatar_id": "laura",
        "summary": "Private durable summary",
        "transcript": "Alex: private transcript",
        "actions": [{"item": "Ship it", "action_id": "a-1"}],
    }

    assert cp.save_artifact(
        org,
        "bot-artifact-restart",
        artifact,
        visibility="private",
        saved_at=saved_at,
    ) is True
    rows = cp.list_artifacts(org)
    assert rows == [
        {
            "bot_id": "bot-artifact-restart",
            "saved_at": saved_at,
            "artifact": artifact,
        }
    ]

    # A fresh engine is the process-replacement boundary. The artifact must not
    # depend on store._artifacts or the App Runner instance filesystem.
    cp.reset_engine()
    assert cp.get_artifact(org, "bot-artifact-restart") == artifact

    with _admin(pg) as conn:
        metadata = conn.execute(
            """
            SELECT visibility, extract(epoch FROM (delete_by - saved_at))
              FROM public.artifacts
             WHERE org_id = %s AND bot_id = %s
            """,
            (org, "bot-artifact-restart"),
        ).fetchone()
    assert metadata[0] == "private"
    assert float(metadata[1]) == pytest.approx(90 * 86400)

    # App Runner's SQLite mirror is ephemeral. Once the PG transaction commits,
    # a local disk failure must not turn success into an endless finalize retry.
    mirror_artifact = {
        "org_id": org,
        "summary": "Postgres remains authoritative",
        "transcript": "Private durable mirror-failure transcript",
    }

    def local_sqlite_down():
        raise RuntimeError("ephemeral SQLite unavailable")

    monkeypatch.setattr(store, "_connect", local_sqlite_down)
    store.save_artifact(
        "bot-local-mirror-down", mirror_artifact, org_id=org
    )
    assert store.get_artifact(
        "bot-local-mirror-down", org_id=org
    ) == mirror_artifact
    store._artifacts.pop("bot-local-mirror-down", None)


def test_durable_artifacts_are_rls_isolated_and_keep_composite_pk(cp, pg):
    from sqlalchemy import text

    org_a = _org(cp, "artifact-a")
    org_b = _org(cp, "artifact-b")
    shared_bot = "bot-same-id"
    artifact_a = {
        "org_id": org_a,
        "summary": "A only",
        "transcript": "tenant A private transcript",
    }
    artifact_b = {
        "org_id": org_b,
        "summary": "B only",
        "transcript": "tenant B private transcript",
    }
    cp.save_artifact(org_a, shared_bot, artifact_a)

    assert cp.get_artifact(org_b, shared_bot) is None
    assert cp.list_artifacts(org_b) == []

    # Even a deliberately hostile UPDATE under B's transaction-local tenant
    # context cannot see or mutate A's row.
    engine = cp._get_engine()
    with engine.begin() as conn:
        cp._set_org(conn, org_b)
        changed = conn.execute(
            text(
                "UPDATE public.artifacts "
                "SET visibility = 'org' "
                "WHERE org_id = CAST(:a AS uuid) AND bot_id = :b"
            ),
            {"a": org_a, "b": shared_bot},
        ).rowcount
    assert changed == 0

    # The documented composite PK permits the same external bot id in two
    # isolated orgs without either row overwriting the other.
    cp.save_artifact(org_b, shared_bot, artifact_b)
    assert cp.get_artifact(org_a, shared_bot) == artifact_a
    assert cp.get_artifact(org_b, shared_bot) == artifact_b

    with _admin(pg) as conn:
        pk_columns = conn.execute(
            """
            SELECT array_agg(a.attname ORDER BY key_column.ordinality)
              FROM pg_constraint AS c
              CROSS JOIN LATERAL
                unnest(c.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
              JOIN pg_attribute AS a
                ON a.attrelid = c.conrelid
               AND a.attnum = key_column.attnum
             WHERE c.conrelid = 'public.artifacts'::regclass
               AND c.contype = 'p'
            """
        ).fetchone()[0]
    assert pk_columns == ["org_id", "bot_id"]
