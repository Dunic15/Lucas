"""Crash-safe action/callback outbox regressions (#138)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app import control_plane, ledger, main, outbox, store, tools
from app.cedric import integration


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "outbox.sqlite3")
    store._init_db()


def _integration(org_id="org-a", *, team="T-1", channel="C-1"):
    return {
        "org_id": org_id,
        "callback_url": "https://surface.example/events",
        "external_ref": {
            "team": team,
            "slack_channel": channel,
            "ignored_secret_like_field": "must-not-persist",
        },
    }


def _due_after_settle(offset: float = 1.0) -> float:
    """Clock safely beyond the intentional live-action settle fence."""
    return time.time() + outbox._ACTION_SETTLE_SECONDS + offset


def test_capture_reload_finalize_keeps_stable_action_id(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(integration, "_kick_outbox", lambda: None)
    session = store.create(
        "bot-capture", "https://meet.google.com/abc-defg-hij", "laura",
        org_id="org-a",
    )
    session.integration = _integration()
    item = tools.capture_action(session, "Send the recap", "Marco", "Friday")

    store._sessions.clear()
    store._load_from_db()
    restored = store.get("bot-capture")
    queued = outbox.queued_actions("org-a", "bot-capture")
    merged = main._merge_action_items(queued, [])

    assert restored is not None
    assert queued == [
        {
            "action_id": item["action_id"],
            "action": "Send the recap",
            "owner": "Marco",
            "due": "Friday",
        }
    ]
    assert merged[0]["action_id"] == item["action_id"]
    store.remove("bot-capture")


def test_transient_5xx_retries_same_idempotency_key_once(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    action = {
        "action_id": "action-1",
        "action": "Send recap",
        "owner": "",
        "due": "",
    }
    outbox.enqueue_action_requested(_integration(), "bot-1", action)
    calls = []

    def fake_post(url, payload, *, idempotency_key=""):
        calls.append((url, payload, idempotency_key))
        code = 500 if len(calls) == 1 else 200
        return SimpleNamespace(status_code=code)

    from app.cedric import callback

    monkeypatch.setattr(callback, "_post", fake_post)
    now = _due_after_settle()
    assert outbox.process_due(now=now) == 0
    assert outbox.process_due(now=now + 2) == 1

    rows = outbox.delivery_rows("org-a")
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered"
    assert rows[0]["attempts"] == 2
    assert [call[2] for call in calls] == [
        "action.requested:action-1",
        "action.requested:action-1",
    ]


def test_permanent_4xx_waits_for_manual_retry(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    outbox.enqueue_action_requested(
        _integration(),
        "bot-permanent",
        {"action_id": "action-permanent", "action": "Send recap"},
    )
    calls = []
    from app.cedric import callback

    def fake_post(url, payload, *, idempotency_key=""):
        calls.append(idempotency_key)
        code = 400 if len(calls) == 1 else 200
        return SimpleNamespace(status_code=code)

    monkeypatch.setattr(callback, "_post", fake_post)
    now = _due_after_settle()
    assert outbox.process_due(now=now) == 0
    row = outbox.delivery_rows("org-a")[0]
    assert row["status"] == "failed"
    assert row["next_attempt_at"] == 0

    # A permanent response stays visible for operator intervention; the
    # periodic worker must not hammer Cedric every five seconds.
    assert outbox.process_due(now=now + 10_000) == 0
    assert calls == ["action.requested:action-permanent"]

    assert outbox.retry("org-a", row["id"])
    assert outbox.process_due(now=now + 10_001) == 1
    assert calls == [
        "action.requested:action-permanent",
        "action.requested:action-permanent",
    ]


def test_scoped_delivery_never_nudges_another_org(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    first = outbox.enqueue_action_requested(
        _integration("org-a"),
        "bot-a",
        {"action_id": "action-a", "action": "A"},
    )
    second = outbox.enqueue_action_requested(
        _integration("org-b"),
        "bot-b",
        {"action_id": "action-b", "action": "B"},
    )
    calls = []
    from app.cedric import callback

    def fake_post(url, payload, *, idempotency_key=""):
        calls.append((payload["org_id"], idempotency_key))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(callback, "_post", fake_post)
    assert outbox.process_due(
        now=_due_after_settle(), org_id="org-a", outbox_id=first
    ) == 1

    assert calls == [("org-a", "action.requested:action-a")]
    assert outbox.delivery_rows("org-a")[0]["status"] == "delivered"
    assert outbox.delivery_rows("org-b")[0]["status"] == "pending"
    assert outbox.delivery_rows("org-b")[0]["id"] == second


def test_customer_callback_without_both_credentials_opens_no_socket(
    monkeypatch,
):
    from app.cedric import callback

    monkeypatch.setattr(callback.secret_registry, "secret_for", lambda org_id: "")
    monkeypatch.setattr(callback.secret_registry, "bearer_for", lambda org_id: "")

    def should_not_open(*args, **kwargs):
        pytest.fail("HTTP client opened before customer credentials were available")

    monkeypatch.setattr(callback.httpx, "Client", should_not_open)
    with pytest.raises(callback.CallbackCredentialsUnavailable):
        callback._post(
            "https://surface.example/events",
            {"event": "action.requested", "org_id": "org-customer"},
            idempotency_key="action.requested:a-1",
        )


def test_redelivery_uses_original_org_team_channel(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    original = _integration("org-original", team="T-original", channel="C-original")
    outbox.enqueue_action_requested(
        original,
        "bot-routing",
        {"action_id": "action-routing", "action": "Create ticket"},
    )
    original["org_id"] = "org-mutated"
    original["external_ref"]["team"] = "T-mutated"

    observed = {}
    from app.cedric import callback

    def fake_post(url, payload, *, idempotency_key=""):
        observed.update({"url": url, "payload": payload, "key": idempotency_key})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(callback, "_post", fake_post)
    assert outbox.process_due(now=_due_after_settle()) == 1
    row = outbox.delivery_rows("org-original")[0]

    assert observed["payload"]["org_id"] == "org-original"
    assert observed["payload"]["external_ref"] == {
        "team": "T-original",
        "slack_channel": "C-original",
    }
    assert row["team_id"] == "T-original"
    assert row["channel"] == "C-original"


def test_duplicate_session_ended_is_one_outbox_event(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    integ = _integration()
    artifact = {
        "summary": "Done",
        "actions": [{"action_id": "a-1", "item": "Send recap"}],
        "transcript": "private transcript",
    }
    first = outbox.enqueue_session_ended(integ, "bot-ended", artifact)
    second = outbox.enqueue_session_ended(integ, "bot-ended", artifact)

    assert first == second
    rows = outbox.delivery_rows("org-a")
    assert len(rows) == 1
    assert rows[0]["event"] == "session.ended"
    with store._LOCK, store._connect() as conn:
        payload = conn.execute(
            "SELECT payload_json FROM callback_outbox WHERE id=?", (first,)
        ).fetchone()["payload_json"]
    assert "private transcript" not in payload


def test_manual_retry_is_idempotent_after_delivery(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    outbox.enqueue_action_requested(
        _integration(),
        "bot-manual",
        {"action_id": "action-manual", "action": "Send recap"},
    )
    from app.cedric import callback

    calls = []
    monkeypatch.setattr(
        callback,
        "_post",
        lambda *a, **k: (
            calls.append(k.get("idempotency_key"))
            or SimpleNamespace(status_code=200)
        ),
    )
    outbox.process_due(now=_due_after_settle())
    row = outbox.delivery_rows("org-a")[0]

    assert outbox.retry("org-a", row["id"])
    assert outbox.process_due(now=time.time() + 2) == 0
    assert calls == ["action.requested:action-manual"]


def test_done_status_cannot_regress_to_approved(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger._init_db()
    assert ledger.set_action_status("action-terminal", "done", org_id="org-a")
    assert ledger.set_action_status("action-terminal", "approved", org_id="org-a")

    status = ledger.action_statuses(
        ["action-terminal"], org_id="org-a"
    )["action-terminal"]
    assert status["status"] == "done"


def test_restart_preserves_outbox_correlation(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    session = store.create(
        "bot-restart", "https://meet.google.com/abc-defg-hij", "laura",
        org_id="org-a",
    )
    session.integration = _integration()
    monkeypatch.setattr(integration, "_kick_outbox", lambda: None)
    item = tools.capture_action(session, "Create the follow-up card")

    store._sessions.clear()
    store._load_from_db()
    outbox.reconcile_sessions()
    rows = outbox.delivery_rows("org-a")

    assert len(rows) == 1
    assert rows[0]["action_id"] == item["action_id"]
    assert rows[0]["bot_id"] == "bot-restart"
    assert rows[0]["team_id"] == "T-1"
    store.remove("bot-restart")



def test_finalize_enqueues_ended_before_local_artifact_and_cleanup(
    tmp_path, monkeypatch
):
    _fresh(tmp_path, monkeypatch)
    session = store.create(
        "bot-order",
        "https://meet.google.com/abc-defg-hij",
        "laura",
        org_id="org-order",
    )
    session.integration = _integration("org-order")
    order: list[str] = []

    monkeypatch.setattr(main.recall_client, "leave_call", lambda bot: None)
    monkeypatch.setattr(
        main.cedric,
        "deliver_ended",
        lambda integration_data, bot, artifact: order.append("pg_enqueue") or True,
    )
    monkeypatch.setattr(
        store,
        "save_artifact",
        lambda *args, **kwargs: order.append("local_artifact"),
    )
    monkeypatch.setattr(
        main.ledger,
        "record_meeting",
        lambda *args, **kwargs: order.append("local_ledger"),
    )
    original_remove = store.remove

    def remove_with_trace(bot):
        order.append("local_cleanup")
        original_remove(bot)

    monkeypatch.setattr(store, "remove", remove_with_trace)
    monkeypatch.setattr(
        main.gpu_runtime, "on_session_ended", lambda count: None
    )
    monkeypatch.setattr(
        main.runpod_runtime, "on_session_ended", lambda count: None
    )

    async def no_close(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "_close_usage_for", no_close)
    artifact = asyncio.run(
        main._finalize_session_locked(
            "bot-order", session, "test", usage_reason="ended"
        )
    )

    assert artifact is not None
    assert order == [
        "pg_enqueue",
        "local_artifact",
        "local_ledger",
        "local_cleanup",
    ]


def test_finalize_keeps_local_session_when_durable_enqueue_fails(
    tmp_path, monkeypatch
):
    _fresh(tmp_path, monkeypatch)
    session = store.create(
        "bot-order-fail",
        "https://meet.google.com/abc-defg-hij",
        "laura",
        org_id="org-order",
    )
    session.integration = _integration("org-order")
    local_writes: list[str] = []

    monkeypatch.setattr(main.recall_client, "leave_call", lambda bot: None)

    def unavailable(*args, **kwargs):
        raise outbox.OutboxUnavailable("simulated")

    monkeypatch.setattr(main.cedric, "deliver_ended", unavailable)
    monkeypatch.setattr(
        store,
        "save_artifact",
        lambda *args, **kwargs: local_writes.append("artifact"),
    )
    original_remove = store.remove
    monkeypatch.setattr(
        store, "remove", lambda bot: local_writes.append("cleanup")
    )

    with pytest.raises(outbox.OutboxUnavailable):
        asyncio.run(
            main._finalize_session_locked(
                "bot-order-fail", session, "test", usage_reason="ended"
            )
        )

    assert local_writes == []
    assert store.get("bot-order-fail") is session
    original_remove("bot-order-fail")



def test_recall_capture_identity_retries_but_later_words_are_distinct():
    base = {
        "event": "transcript.data",
        "data": {
            "bot": {"id": "bot-identity"},
            "transcript": {"id": "tr-1"},
            "recording": {"id": "rec-1"},
            "realtime_endpoint": {"id": "ep-1"},
        },
    }
    participant = {"id": 7, "name": "Alex"}
    words = [
        {
            "text": "Laura,",
            "start_timestamp": {"relative": 12.0},
            "end_timestamp": {"relative": 12.4},
        },
        {
            "text": "send the recap",
            "start_timestamp": {"relative": 12.5},
            "end_timestamp": {"relative": 13.2},
        },
    ]
    kwargs = {
        "signed": False,
        "org_id": "org-a",
        "bot_id": "bot-identity",
        "participant": participant,
        "text": "Laura, send the recap",
    }
    first = main._recall_capture_identity(base, {}, words=words, **kwargs)
    retry = main._recall_capture_identity(base, {}, words=list(words), **kwargs)
    later_words = [dict(word) for word in words]
    later_words[0] = {
        **later_words[0],
        "start_timestamp": {"relative": 52.0},
    }
    later = main._recall_capture_identity(
        base, {}, words=later_words, **kwargs
    )

    assert first == retry
    assert first[0]
    assert later[0] != first[0]


def test_sqlite_fallback_fingerprint_is_bounded(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(integration, "_kick_outbox", lambda: None)
    session = store.create(
        "bot-fallback", "https://meet.google.com/abc-defg-hij", "laura",
        org_id="org-a",
    )
    session.integration = _integration()
    first, created = tools.capture_action_once(
        session,
        "Send the recap",
        source_fingerprint="d" * 64,
        dedupe_window_seconds=30,
    )
    replay, replay_created = tools.capture_action_once(
        session,
        "Send the recap",
        source_fingerprint="d" * 64,
        dedupe_window_seconds=30,
    )
    assert created is True and replay_created is False
    assert replay["action_id"] == first["action_id"]

    with store._LOCK, store._connect() as conn:
        conn.execute(
            "UPDATE queued_actions SET created_at=created_at-60 "
            "WHERE org_id=? AND action_id=?",
            ("org-a", first["action_id"]),
        )
    later, later_created = tools.capture_action_once(
        session,
        "Send the recap",
        source_fingerprint="d" * 64,
        dedupe_window_seconds=30,
    )
    assert later_created is True
    assert later["action_id"] != first["action_id"]



def test_duplicate_continuation_updates_pending_wire_once(
    tmp_path, monkeypatch
):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(integration, "_kick_outbox", lambda: None)
    session = store.create(
        "bot-continuation", "https://meet.google.com/abc-defg-hij", "laura",
        org_id="org-a",
    )
    session.integration = _integration()
    item, created = tools.capture_action_once(
        session,
        "Send the recap",
        source_event_key="initial-event",
    )
    assert created is True

    first, applied = tools.extend_action_once(
        session,
        item,
        "by Friday",
        source_event_key="continuation-event",
    )
    replay, replay_applied = tools.extend_action_once(
        session,
        item,
        "by Friday",
        source_event_key="continuation-event",
    )
    assert applied is True and replay_applied is False
    assert first["action"] == replay["action"] == "Send the recap by Friday"

    # The initial worker nudge cannot deliver the incomplete first fragment.
    sent = []
    from app.cedric import callback

    monkeypatch.setattr(
        callback,
        "_post",
        lambda url, payload, *, idempotency_key="": (
            sent.append((payload["action"], idempotency_key))
            or SimpleNamespace(status_code=200)
        ),
    )
    now = time.time()
    assert outbox.process_due(now=now + 1) == 0
    assert outbox.process_due(now=now + 10) == 1
    assert sent == [
        (
            "Send the recap by Friday",
            f"action.requested:{item['action_id']}",
        )
    ]
    with store._LOCK, store._connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM action_capture_events"
        ).fetchone()[0] == 1



def test_initial_final_replay_preserves_next_continuation_window(
    tmp_path, monkeypatch
):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(integration, "_kick_outbox", lambda: None)
    session = store.create(
        "bot-initial-replay",
        "https://meet.google.com/abc-defg-hij",
        "cedric",
        org_id="org-a",
    )
    session.integration = _integration()
    item, created = tools.capture_action_once(
        session,
        "Send the recap",
        source_event_key="initial-final-key",
    )
    assert created is True
    session.last_capture = (
        item, "Alex", time.time(), "initial-final-key", "initial-fingerprint"
    )

    # What the webhook's same_source fast path guarantees: replay keeps the
    # full identity-bearing state instead of clearing it before durable dedupe.
    pending = session.last_capture
    assert pending[3:] == ("initial-final-key", "initial-fingerprint")

    # The genuinely next split uses a different source id and still appends.
    updated, applied = tools.extend_action_once(
        session,
        item,
        "by Friday",
        source_event_key="continuation-final-key",
    )
    session.last_capture = (
        updated, "Alex", time.time(),
        "continuation-final-key", "continuation-fingerprint",
    )
    assert applied is True
    assert updated["action"] == "Send the recap by Friday"



def test_crash_after_ended_checkpoint_reuses_first_wire_action_ids(
    tmp_path, monkeypatch
):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(integration, "_kick_outbox", lambda: None)
    integ = _integration("org-checkpoint")
    first = {
        "summary": "First canonical summary",
        "actions": [{"action_id": "action-first", "item": "Send recap"}],
        "checklist": [{"action_id": "action-first", "item": "Send recap"}],
        "transcript": "local transcript first",
        "meeting_url": "https://meet.google.com/private",
        "org_id": "org-checkpoint",
    }
    assert integration.deliver_ended(integ, "bot-checkpoint", first) is True

    rebuilt_after_crash = {
        "summary": "Second nondeterministic summary",
        "actions": [{"action_id": "action-second", "item": "Send recap"}],
        "checklist": [{"action_id": "action-second", "item": "Send recap"}],
        "transcript": "local transcript preserved on retry",
        "meeting_url": "https://meet.google.com/private",
        "org_id": "org-checkpoint",
    }
    assert integration.deliver_ended(
        integ, "bot-checkpoint", rebuilt_after_crash
    ) is True

    assert rebuilt_after_crash["summary"] == "First canonical summary"
    assert rebuilt_after_crash["actions"][0]["action_id"] == "action-first"
    assert rebuilt_after_crash["checklist"][0]["action_id"] == "action-first"
    assert rebuilt_after_crash["transcript"] == "local transcript preserved on retry"
    assert rebuilt_after_crash["meeting_url"] == "https://meet.google.com/private"

    with store._LOCK, store._connect() as conn:
        payloads = conn.execute(
            "SELECT payload_json FROM callback_outbox "
            "WHERE org_id=? AND bot_id=? AND event='session.ended'",
            ("org-checkpoint", "bot-checkpoint"),
        ).fetchall()
    assert len(payloads) == 1
    assert '"action-first"' in payloads[0]["payload_json"]
    assert '"action-second"' not in payloads[0]["payload_json"]


def test_finalize_keeps_session_when_artifact_postgres_is_down(
    tmp_path, monkeypatch
):
    _fresh(tmp_path, monkeypatch)
    bot_id = "bot-artifact-pg-down"
    store._artifacts.pop(bot_id, None)
    session = store.create(
        bot_id,
        "https://meet.google.com/abc-defg-hij",
        "laura",
        org_id="org-artifact-pg-down",
    )
    session.integration = _integration("org-artifact-pg-down")

    monkeypatch.setattr(main.recall_client, "leave_call", lambda bot: None)
    monkeypatch.setattr(main.outbox, "begin_action_finalize", lambda *args: [])
    monkeypatch.setattr(main.cedric, "deliver_ended", lambda *args: True)
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(
        store.settings,
        "laura_database_url",
        "postgresql://configured-artifact-test",
    )

    def postgres_down(*args, **kwargs):
        raise RuntimeError("durable artifact database unavailable")

    monkeypatch.setattr(control_plane, "save_artifact", postgres_down)

    with pytest.raises(
        RuntimeError, match="durable artifact database unavailable"
    ):
        asyncio.run(
            main._finalize_session_locked(
                bot_id, session, "test", usage_reason="ended"
            )
        )

    # PG-first means no local false-success and, because cleanup is after the
    # durable write, the session remains available for a bounded retry.
    assert store.get(bot_id) is session
    assert store.get_artifact(bot_id) is None
    store.remove(bot_id)


def test_keyfree_artifact_sqlite_behavior_is_unchanged(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    bot_id = "bot-keyfree-artifact"
    org_id = "org-keyfree"
    artifact = {
        "org_id": org_id,
        "summary": "Local demo summary",
        "transcript": "Local demo transcript",
    }
    monkeypatch.setattr(control_plane, "enabled", lambda: False)

    def must_not_call_postgres(*args, **kwargs):
        raise AssertionError("key-free artifact path touched Postgres")

    monkeypatch.setattr(control_plane, "save_artifact", must_not_call_postgres)
    store.save_artifact(bot_id, artifact, org_id=org_id)

    # Rebuild process memory from SQLite, matching the existing demo restart
    # path, then exercise both legacy get and tenant-filtered list shapes.
    store._artifacts.clear()
    store._load_from_db()
    assert store.get_artifact(bot_id) == artifact
    assert store.list_artifacts(org_id) == [
        {
            "bot_id": bot_id,
            "saved_at": pytest.approx(time.time(), abs=5),
            "artifact": artifact,
        }
    ]
    store._artifacts.pop(bot_id, None)


def test_finalize_idempotent_artifact_read_is_org_scoped(
    tmp_path, monkeypatch
):
    _fresh(tmp_path, monkeypatch)
    bot_id = "bot-finished-org-a"
    artifact = {
        "org_id": "org-a",
        "summary": "A private summary",
        "transcript": "A private transcript",
    }
    monkeypatch.setattr(control_plane, "enabled", lambda: False)
    store.save_artifact(bot_id, artifact, org_id="org-a")
    store._sessions.pop(bot_id, None)

    # This is the exact session-missing path reached by a repeated /end. A
    # trusted org-B principal gets not-found, never A's cached artifact.
    assert asyncio.run(
        main._finalize_session(
            bot_id, source="test", artifact_org_id="org-b"
        )
    ) is None
    assert asyncio.run(
        main._finalize_session(
            bot_id, source="test", artifact_org_id="org-a"
        )
    ) == artifact
    store._artifacts.pop(bot_id, None)
