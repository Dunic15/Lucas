"""Laura-native Action Runtime: adapter selection, connection truth, receipts."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import action_plane, executor, native_runtime, store  # noqa: E402


def test_runtime_supports_all_current_native_writes():
    assert native_runtime.action_types() == frozenset(
        {
            "calendar.create_event",
            "email.send",
            "asana.create_task",
            "asana.update_task",
            "asana.add_comment",
        }
    )
    assert executor.NATIVE_ACTION_TYPES == native_runtime.action_types()
    assert executor.capability_family("slack.post_message") == "slack"


def test_from_typed_accepts_only_registered_adapters():
    assert native_runtime.from_typed(
        {"type": "slack.post_message", "args": {"text": "Recap"}}
    ) is None
    assert executor.from_typed(
        {"type": "slack.post_message", "args": {"text": "Recap"}}
    ) == {"type": "slack.post_message", "message": {"text": "Recap"}}
    assert native_runtime.from_typed(
        {"type": "notion.create_page", "args": {"title": "No adapter"}}
    ) is None


def test_slack_is_never_native_and_routes_per_org(monkeypatch):
    typed = {"type": "slack.post_message", "args": {"text": "Meeting recap"}}
    assert native_runtime.supports("slack.post_message") is False

    monkeypatch.setattr(
        store,
        "connections_for_org",
        lambda org: [{"provider": "cedric-brain", "status": "connected"}],
    )
    assert executor.route_for_typed(typed, "org-a") == "cedric"

    monkeypatch.setattr(store, "connections_for_org", lambda org: [])
    assert executor.route_for_typed(typed, "org-a") == "manual"

    result = native_runtime.execute(
        "org-a", {"type": "slack.post_message", "args": {"text": "x"}}
    )
    assert result["ok"] is False
    assert "no native adapter" in result["error"]


def test_unknown_tool_never_falls_back_to_external_executor():
    result = native_runtime.execute(
        "org-a", {"type": "notion.create_page", "args": {"title": "x"}}
    )
    assert result["ok"] is False
    assert "no native adapter" in result["error"]


def test_executor_persists_laura_native_receipt(monkeypatch):
    statuses: list[tuple] = []
    events: list[tuple] = []

    monkeypatch.setattr(settings, "native_executor", True)
    monkeypatch.setattr(
        executor.native_runtime,
        "execute",
        lambda org, action: {
            "ok": True,
            "kind": "calendar event",
            "ref": "https://calendar.test/event/1",
        },
    )
    monkeypatch.setattr(
        executor.ledger,
        "set_action_status",
        lambda *args, **kwargs: statuses.append((args, kwargs)) or True,
    )

    from app.cedric import callback as slack_surface

    monkeypatch.setattr(
        slack_surface,
        "send_action_event",
        lambda *args, **kwargs: events.append((args, kwargs)) or True,
    )

    result = executor.execute_approved(
        "org-a",
        "act-1",
        {
            "type": "calendar.create_event",
            "event": {
                "title": "Follow-up",
                "start": "2026-08-01T10:00:00+02:00",
                "end": "2026-08-01T10:30:00+02:00",
            },
        },
    )

    assert result["ok"] is True
    assert statuses
    _args, kwargs = statuses[0]
    assert kwargs["receipt"]["runtime"] == "laura"
    assert kwargs["receipt"]["route"] == "native"
    assert events  # status projection only; execution already happened locally


def test_slack_action_schema_requires_text():
    typed = {"type": "slack.post_message", "args": {}}
    assert action_plane.missing_params(typed) == ["text"]
    assert action_plane.risk_for(typed) == "medium"
