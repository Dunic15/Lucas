"""Deterministic capability truth + grounding on the live path (2026-07-23).

Live test: within one meeting Petra claimed Asana/Jira/Calendar/Gmail/Drive
connected, then "I don't have access to your Asana", then "I can't access
external applications", and web-searched "which actions can you do in Gmail?".
These lock the fix: capability questions resolve to a deterministic org-scoped
answer (never web search, never a model-invented roster), and the answer keeps
connected / snapshot / executable as separate states. Key-free — no vendors.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import capabilities as cap  # noqa: E402
from app.brain import engine  # noqa: E402
from app.config import settings  # noqa: E402


# ── the exact regression transcript ─────────────────────────────────────
_REGRESSION = [
    "Which tools can you use?",
    "Which actions can you do in Google or in Gmail?",
    "Do you have a snapshot of my Asana?",
    "What's in my zone at the moment?",  # ASR of "in my Asana" (context-bound)
    "Do you have access to my Google Drive?",
    "What do you actually see in Drive?",
]


@pytest.mark.parametrize("utter", _REGRESSION[:3] + _REGRESSION[4:])
def test_capability_questions_are_classified(utter):
    # (index 3, the "zone" ASR line, is a workspace read after repair — covered
    # separately; the rest must all read as capability/self questions.)
    assert cap.is_capability_question(utter), utter


@pytest.mark.parametrize("utter", _REGRESSION)
def test_capability_utterances_never_web_search(monkeypatch, utter):
    """The whole sequence, incl. the exact 'which action … in Gmail' line, must
    NEVER trigger a public web search."""
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    assert engine.wants_web_search(utter) is False, utter


def test_which_action_in_gmail_is_a_self_question(monkeypatch):
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    q = "Which action can you do in Google or in Gmail?"
    assert cap.is_capability_question(q)
    assert engine._is_about_avatar(q)          # defense-in-depth gate
    assert engine.wants_web_search(q) is False  # never a web search


def test_ordinary_web_search_still_works(monkeypatch):
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "synthetic")
    for q in ("can you search online for the latest news",
              "look it up on the web", "what's the latest funding round for Acme"):
        assert not cap.is_capability_question(q), q


# ── the deterministic answer keeps the four states separate ─────────────
def _snap(**overrides):
    base = {
        "asana_tasks": dict(connected_for_org=True, enabled_for_avatar=True,
                            live_health="ok", snapshot_available_in_meeting=False,
                            can_read_now=False, can_execute_now=True,
                            execution_route="pipedream",
                            supported_verbs=["create tasks", "update tasks"],
                            unavailable_reason="no snapshot"),
        "gmail_send": dict(connected_for_org=True, enabled_for_avatar=True,
                           live_health="ok", snapshot_available_in_meeting=None,
                           can_read_now=True, can_execute_now=True,
                           execution_route="native",
                           supported_verbs=["send email"], unavailable_reason=""),
        "google_drive": dict(connected_for_org=False, enabled_for_avatar=False,
                             live_health="unconfigured",
                             snapshot_available_in_meeting=None, can_read_now=False,
                             can_execute_now=False, execution_route="",
                             supported_verbs=[], unavailable_reason="not connected"),
    }
    base.update(overrides)
    return {"tools": base}


def test_answer_separates_connected_from_snapshot():
    a = cap.answer("do you have a snapshot of my asana?", _snap()).lower()
    assert "asana is connected" in a          # (1) connected
    assert "create tasks" in a                # (4) can execute
    assert "snapshot" in a and "can't read" in a  # (3) no snapshot → honest
    # never the flat wrong claim
    assert "i don't have access to asana" not in a
    assert "can't access external" not in a


def test_answer_never_invents_a_tool():
    """No phantom Jira: the roster names only what's actually connected."""
    a = cap.answer("which tools can you use?", _snap()).lower()
    assert "jira" not in a
    assert "asana" in a and "gmail" in a
    assert "google drive" not in a  # drive is unconnected → not claimed


def test_answer_unconnected_tool_is_honest():
    a = cap.answer("can you read my drive?", _snap()).lower()
    assert "isn't connected" in a
    assert "i can see your drive" not in a  # never a fabricated inventory


# ── context-bound ASR repair ────────────────────────────────────────────
def test_asr_repair_only_with_supporting_context():
    # Asana just discussed → "my zone" is repaired.
    assert "asana" in cap.repair_asr(
        "what's in my zone at the moment?", "Duccio: can you check my asana"
    ).lower()
    # No Asana in context → left untouched (never a global rewrite).
    assert cap.repair_asr(
        "what's in my zone at the moment?", "Duccio: how's the weather"
    ) == "what's in my zone at the moment?"


# ── grounding rules present in the spoken prompt ────────────────────────
def test_grounding_state_distinctions_are_in_the_prompt():
    prompt = engine.ANSWER_STREAM_SYSTEM.format(persona="P", name="Petra")
    assert "Keep FOUR states separate" in prompt
    assert 'never "I don\'t have access to Asana"' in prompt
    assert 'Never say "the meeting hasn\'t started"' in prompt
    assert "no phantom Jira" in prompt
