"""The Slack-approval -> EXECUTION bridge: POST /org/actions/{id}/approve runs
the SAME native executor the dashboard approve door runs, with decision-based
idempotency so a Slack+dashboard double-approve never executes twice.

Closes the gap where clicking Approve in Slack only updated the message and
never ran the action. Key-free like the rest of the suite (the machine gate
resolves the Demo org when no bearer is configured)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import executor, ledger, store
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    monkeypatch.setattr(settings, "native_executor", True, raising=False)
    return TestClient(main_module.app)


def _seed_typed_calendar_action(action_id: str) -> None:
    """An artifact whose action carries a TYPED calendar.create_event spec — the
    trusted, executable shape the approve door reads (never the request body)."""
    artifact = {
        "summary": "Weekly sync",
        "actions": [{
            "action_id": action_id,
            "item": "Book the follow-up with Ananth",
            "owner": "duccio",
            "typed": {"type": "calendar.create_event", "args": {
                "title": "Follow-up", "start": "2026-07-20T15:00:00+02:00",
                "end": "2026-07-20T15:30:00+02:00", "attendees": ["ananth@sffstudio.com"]}},
        }],
        "checklist": [], "decisions": [], "missing_steps": [],
        "avatar_id": "cedric",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "duration_seconds": 600,
    }
    store.save_artifact("bot-approve-1", artifact)
    ledger.record_meeting(
        meeting_url="https://meet.google.com/abc-defg-hij", avatar_id="cedric",
        bot_id="bot-approve-1", artifact=artifact,
    )


def _spy_executor(monkeypatch) -> list:
    """Replace the real integration call with a spy that records each run and
    writes the same 'done' receipt the real executor would."""
    calls: list = []

    def fake_execute_approved(org, action_id, action):
        calls.append((org, action_id, action.get("type")))
        ledger.set_action_status(action_id, "done", "event created", org_id=org)
        return {"ok": True, "event_id": "evt-1"}

    monkeypatch.setattr(executor, "execute_approved", fake_execute_approved)
    return calls


def test_slack_approve_executes_the_action(client, monkeypatch):
    calls = _spy_executor(monkeypatch)
    _seed_typed_calendar_action("act-approve-1")
    r = client.post("/org/actions/act-approve-1/approve", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["approved"] and body["executed"] is True
    assert body["idempotent_replay"] is False
    assert len(calls) == 1  # executed exactly once


def test_repeat_approve_never_double_executes(client, monkeypatch):
    calls = _spy_executor(monkeypatch)
    _seed_typed_calendar_action("act-approve-2")
    first = client.post("/org/actions/act-approve-2/approve", json={"decision": "approve"})
    assert first.json()["executed"] is True
    # A second approval (e.g. from the dashboard after Slack) must REPLAY, not
    # create a second calendar event.
    second = client.post("/org/actions/act-approve-2/approve", json={"decision": "approve"})
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert len(calls) == 1  # still exactly one execution


def test_reject_then_approve_is_a_conflict(client, monkeypatch):
    calls = _spy_executor(monkeypatch)
    _seed_typed_calendar_action("act-approve-3")
    rej = client.post("/org/actions/act-approve-3/approve", json={"decision": "reject"})
    assert rej.status_code == 200 and rej.json()["new_status"] == "rejected"
    conflict = client.post("/org/actions/act-approve-3/approve", json={"decision": "approve"})
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "decision_conflict"
    assert len(calls) == 0  # a rejected action never executes


def test_unknown_action_is_404(client, monkeypatch):
    _spy_executor(monkeypatch)
    r = client.post("/org/actions/nope-nope/approve", json={"decision": "approve"})
    assert r.status_code == 404


def test_approve_respects_bearer_gate(client, monkeypatch):
    """With a configured token, an unauthenticated machine call is refused —
    the same gate as /resolve and /status."""
    monkeypatch.setattr(settings, "laura_api_token", "sekret")
    _seed_typed_calendar_action("act-approve-4")
    denied = client.post("/org/actions/act-approve-4/approve", json={"decision": "approve"})
    assert denied.status_code in (401, 403)
