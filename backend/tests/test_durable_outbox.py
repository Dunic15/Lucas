"""Crash-safe action/callback outbox regressions (#138)."""
from __future__ import annotations

import time
from types import SimpleNamespace

from app import ledger, main, outbox, store, tools
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
    now = time.time() + 1
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
    now = time.time() + 1
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
    assert outbox.process_due(now=time.time() + 1) == 1
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
    outbox.process_due(now=time.time() + 1)
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
