"""Voice-consent writes: an ADDRESSED mid-meeting ask ("Petra, create a task
for X") is the approval — recorded on the canonical channel
(decided_via='voice') and pushed to Cedric as action.approved to execute NOW,
instead of parking a card until after the call.

Covers: the unit seam (cedric.voice_approve — record + notify, first-write-
wins, never double-fires over an existing decision), the live webhook wiring
(capture branch → voice_approve + the approved-and-running spoken line), and
the flag-off legacy behaviour. Key-free like the rest of the suite.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import cedric, ledger, store
from app.cedric import callback as cedric_callback
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


@pytest.fixture
def recall_stubbed(monkeypatch):
    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(
        main_module.recall_client,
        "create_bot",
        lambda meeting_url, avatar_page_url, join_at=None, bot_name="Laura",
        avatar_id="": {"id": "bot_vc"},
    )
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda bot_id: None)
    monkeypatch.setattr(main_module.recall_client, "delete_bot", lambda bot_id: None)
    monkeypatch.setattr(
        main_module.anam_client, "end_conversation", lambda conv_id: None
    )


@pytest.fixture
def spoken(monkeypatch):
    lines: list[str] = []

    async def fake_speak(session, line, **kwargs):
        lines.append(line)
        return True

    monkeypatch.setattr(main_module, "_make_avatar_speak", fake_speak)
    return lines


START_BODY = {
    "meeting_url": "https://meet.google.com/vc-test-one",
    "avatar_id": "cedric",
    "callback_url": "https://cedric.example/api/meet/callback",
    "external_ref": {"team": "T1"},
}


def _transcript(bot_id: str, text: str) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": w} for w in text.split()],
                "participant": {"name": "Ben", "id": 1},
            },
        },
    }


ITEM = {"action_id": "act_vc1", "action": "Create a task to send the recap",
        "owner": "Dana", "due": "Friday"}


# ─────────────────────── unit: cedric.voice_approve ───────────────────────
def test_voice_approve_records_and_notifies(client, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        cedric_callback, "send_action_event",
        lambda org, event, fields: sent.append((org, event, fields)) or True,
    )
    session = store.create("bot_u1", "https://meet.example/u1", "cedric", org_id="org_v")
    session.integration = {"org_id": "org_v"}

    assert cedric.voice_approve(session, dict(ITEM)) is True

    row = store.get_action_approval("org_v", "act_vc1")
    assert row and row["decision"] == "approve" and row["decided_via"] == "voice"
    status = ledger.action_statuses(["act_vc1"], org_id="org_v").get("act_vc1")
    assert status and status["status"] == "approved"
    org, event, fields = sent[0]
    assert org == "org_v" and event == "action.approved"
    assert fields["action_id"] == "act_vc1" and fields["decided_via"] == "voice"
    assert fields["action"].startswith("Create a task")


def test_voice_approve_never_double_fires(client, monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        cedric_callback, "send_action_event", lambda *a: sent.append(a) or True
    )
    session = store.create("bot_u2", "https://meet.example/u2", "cedric", org_id="org_w")
    session.integration = {"org_id": "org_w"}
    # Another surface (dashboard) decided FIRST — reject, say.
    store.record_action_approval(
        "org_w", "act_vc2", decision="reject", decided_via="dashboard"
    )
    item = dict(ITEM, action_id="act_vc2")
    assert cedric.voice_approve(session, item) is False
    assert sent == []  # no execute signal on top of someone else's decision
    row = store.get_action_approval("org_w", "act_vc2")
    assert row["decision"] == "reject"  # first write still wins


def test_voice_approve_needs_action_id(client, monkeypatch):
    monkeypatch.setattr(
        cedric_callback, "send_action_event",
        lambda *a: pytest.fail("must not fire without an action_id"),
    )
    session = store.create("bot_u3", "https://meet.example/u3", "cedric")
    assert cedric.voice_approve(session, {"action": "no id"}) is False


# ──────────────── live webhook wiring (the capture branch) ────────────────
def test_addressed_ask_voice_approves_and_says_so(
    client, recall_stubbed, spoken, monkeypatch
):
    monkeypatch.setattr(settings, "clarify_before_create", False)
    monkeypatch.setattr(settings, "voice_consent_writes", True)  # opt-in flavour
    approved: list[dict] = []
    monkeypatch.setattr(
        main_module.cedric, "voice_approve",
        lambda session, item: approved.append(dict(item)) or True,
    )
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = client.post(
        "/webhooks/recall",
        json=_transcript(bot_id, "Cedric, please create a task to send the recap to Marco"),
    ).json()
    assert body.get("action_capture") is True
    assert len(approved) == 1
    assert "create a task" in approved[0]["action"].lower()
    voice_pool = main_module._VOICE_LINES + main_module._VOICE_LINES_IT
    assert spoken and spoken[-1] in voice_pool


def test_voice_consent_default_is_off():
    """The approval-first contract (owner ask 2026-07-18): by default nothing
    executes on a spoken ask alone — every action waits for an explicit
    approval. Voice consent is the env opt-in, never the default."""
    from app.config import Settings

    assert Settings.model_fields["voice_consent_writes"].default is False


def test_flag_off_keeps_the_queue_flow(client, recall_stubbed, spoken, monkeypatch):
    monkeypatch.setattr(settings, "clarify_before_create", False)
    monkeypatch.setattr(settings, "voice_consent_writes", False)
    monkeypatch.setattr(
        main_module.cedric, "voice_approve",
        lambda *a: pytest.fail("voice_approve must not run with the flag off"),
    )
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = client.post(
        "/webhooks/recall",
        json=_transcript(bot_id, "Cedric, please create a task to send the recap to Marco"),
    ).json()
    assert body.get("action_capture") is True
    queue_pool = main_module._QUEUE_LINES + main_module._QUEUE_LINES_IT
    assert spoken and spoken[-1] in queue_pool
