"""Clarify-before-create: an addressed create-ask that lacks what a well-
filed task needs (owner / project / due) makes the avatar ASK once, hold the
approval, and resolve on the asker's reply — no more tasks born ownerless and
invisible. Plus the executor-side net: an Asana task created with no assignee
defaults to the connected account ("me"), so nothing can be orphaned again.

Key-free like the rest of the suite.
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import asana_client, ledger, store, tools
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
        avatar_id="": {"id": "bot_cl"},
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


@pytest.fixture
def approved(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        main_module.cedric, "voice_approve",
        lambda session, item: calls.append(dict(item)) or True,
    )
    return calls


START_BODY = {
    "meeting_url": "https://meet.google.com/cl-test-one",
    "avatar_id": "cedric",
    "callback_url": "https://cedric.example/api/meet/callback",
}


def _say(client, bot_id: str, text: str) -> dict:
    return client.post(
        "/webhooks/recall",
        json={
            "event": "transcript.data",
            "data": {
                "bot": {"id": bot_id},
                "data": {
                    "words": [{"text": w} for w in text.split()],
                    "participant": {"name": "Ben", "id": 1},
                },
            },
        },
    ).json()


def _age_clarify(session, seconds: float = 10.0) -> None:
    """Move the clarify timestamp back so the next line reads as an ANSWER
    (in real meetings the avatar's spoken question already consumed >4s)."""
    it, spk, ts, missing, ek, fp = session.pending_clarify
    session.pending_clarify = (it, spk, ts - seconds, missing, ek, fp)


# ───────────────────────── the clarify loop ─────────────────────────
def test_full_details_skip_clarify(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(
        client, bot_id,
        "Cedric, create a task to send the recap, assigned to Dana, "
        "in the launch project, due Friday",
    )
    assert body.get("action_capture") is True
    assert "clarifying" not in body
    assert len(approved) == 1  # nothing missing → approved immediately
    assert spoken[-1] in main_module._VOICE_LINES + main_module._VOICE_LINES_IT


def test_missing_details_ask_then_answer(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(client, bot_id, "Cedric, please create a task called help ducho")
    assert body.get("clarifying") == ["owner", "project", "due"]
    assert approved == []  # held — nothing approved yet
    assert spoken[-1].startswith("Sure — before I create it:")
    assert "who should own it" in spoken[-1] and "which project" in spoken[-1]

    session = store.get(bot_id)
    _age_clarify(session)
    body = _say(
        client, bot_id,
        "Dana should take it, put it in the launch project, due Monday",
    )
    assert body.get("clarified") is True
    assert len(approved) == 1
    action_text = session.queued_actions[0]["action"].lower()
    assert "help ducho" in action_text and "dana" in action_text
    assert spoken[-1] in main_module._VOICE_LINES + main_module._VOICE_LINES_IT


def test_skip_answer_proceeds_as_is(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    _say(client, bot_id, "Cedric, create a task called triage the inbox")
    session = store.get(bot_id)
    _age_clarify(session)
    original = session.queued_actions[0]["action"]

    body = _say(client, bot_id, "no one, just create it")
    assert body.get("clarified") is True
    assert len(approved) == 1
    assert session.queued_actions[0]["action"] == original  # skip words not glued on


def test_new_ask_resolves_stale_pending(client, recall_stubbed, spoken, approved):
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    _say(client, bot_id, "Cedric, create a task called first thing")
    session = store.get(bot_id)
    # The asker never answers — they fire a NEW complete ask instead.
    _age_clarify(session, seconds=50.0)  # also past the answer window
    body = _say(
        client, bot_id,
        "Cedric, create a task to email Marco, assigned to Dana, "
        "in the launch project, by Friday",
    )
    assert body.get("action_capture") is True
    # BOTH resolved: the stale one quietly, the new one on its merits.
    assert len(approved) == 2
    texts = " | ".join(a["action"] for a in approved).lower()
    assert "first thing" in texts and "email marco" in texts


def test_flag_off_keeps_immediate_confirmation(
    client, recall_stubbed, spoken, approved, monkeypatch
):
    monkeypatch.setattr(settings, "clarify_before_create", False)
    bot_id = client.post("/sessions/start", json=START_BODY).json()["bot_id"]
    body = _say(client, bot_id, "Cedric, create a task called bare minimum")
    assert "clarifying" not in body
    assert len(approved) == 1


# ───────────────────────── the detail heuristics ─────────────────────────
def test_missing_action_details_cases():
    m = tools.missing_action_details
    assert m("create a task called help ducho") == ["owner", "project", "due"]
    assert m("create a task, assigned to Dana, in the launch project, due Friday") == []
    assert "owner" not in m("Dana will own the recap task")
    assert "due" not in m("send it by tomorrow")
    assert "project" not in m("put it on the growth board")
    assert m("") == ["owner", "project", "due"]


def test_is_detail_skip_cases():
    assert tools.is_detail_skip("no one, just create it")
    assert tools.is_detail_skip("doesn't matter, skip it")
    assert not tools.is_detail_skip("Dana should own it")


# ──────────────── executor net: no task is born invisible ────────────────
def test_create_task_defaults_assignee_to_me(monkeypatch):
    posted: list[dict] = []
    monkeypatch.setattr(asana_client, "_token", lambda org: ("pat_x", ""))
    monkeypatch.setattr(asana_client, "_workspace_gid", lambda pat: ("ws1", ""))
    monkeypatch.setattr(asana_client, "_resolve_project", lambda org, p: ("", ""))
    monkeypatch.setattr(
        asana_client, "_post",
        lambda pat, path, body, **kw: posted.append(body)
        or {"ok": True, "task": {"gid": "t1"}},
    )
    asana_client.create_task("org_x", {"name": "Help Ducho"})
    assert posted[0]["assignee"] == "me"  # never ownerless-and-invisible again

    asana_client.create_task("org_x", {"name": "Other", "assignee": "dana@x.com"})
    assert posted[1]["assignee"] == "dana@x.com"  # explicit assignee always wins
