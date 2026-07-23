"""Simulated-meeting matrix: does each spoken line become the RIGHT thing?

Built from a harness run over real + adversarial utterances (2026-07-22) that
found four live gaps on main:

  1. The 20-action expansion (#375) made Drive/Gmail/Calendar edits EXECUTABLE
     while capture never learned to hear them — Laura could run
     drive.share_file / gmail.archive / calendar.cancel_event and still never
     turn the spoken ask into a card.
  2. "add Marco to that calendar invite" — the add-branch accepted only a bare
     surface noun after the/my/our, so demonstratives and invite/event targets
     were dropped.
  3. "Okay. And then can you send, an" still produced a card (live card
     64dcf5c0 → "Send, an to"): a truncated imperative names no object.
  4. "are you connected to Slack?" was not a self-question, so the model
     improvised "No" while Slack WAS linked to the org.

Each row asserts the two deterministic live-path decisions, plus the standing
invariant that a self/capability question NEVER reaches the public web.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine

# (utterance, becomes_card, is_self_question)
CASES: list[tuple[str, bool, bool]] = [
    # ── real asks that MUST become cards ────────────────────────────────
    ("can you schedule a meeting with Anant tomorrow at 3pm?", True, False),
    ("send the recap email to daniel@example.com", True, False),
    ("create a task in Asana for the Pipedream connection", True, False),
    ("please book a follow-up with the design team next Tuesday", True, False),
    ("can you create a folder in Drive for the Q3 launch?", True, False),
    ("draft an email to the investors summarising this call", True, False),
    ("add Marco to that calendar invite", True, False),
    ("archive the emails from noreply@vendor.example", True, False),
    ("cancel the meeting with Priya on Friday", True, False),
    ("share the deck with anant@example.com", True, False),
    ("rename the file to Q3-final", True, False),
    ("reply to that email saying we're in", True, False),
    ("puoi mandare una mail a Jacopo con il riassunto?", True, False),
    ("puoi creare un task su Asana per lunedì?", True, False),

    # ── fragments / questions that must NOT become cards ────────────────
    ("Schedule in my calendar? What schedule?", False, False),
    ("can you send can you schedule a meeting? What what are in my calendar",
     False, False),
    ("Okay. And then can you send, an", False, False),
    ("can you send, an", False, False),
    ("okay so schedule a", False, False),
    ("what are the events in my calendar this week?", False, False),
    ("did you schedule it?", False, False),

    # ── conversational look-alikes: never actions ───────────────────────
    ("let's move on to the next topic", False, False),
    ("can you share your screen?", False, False),
    ("we should send them the deck at some point", False, False),
    ("I'll email him later", False, False),

    # ── self/capability questions: roster, never the public web ─────────
    ("what can you do?", False, True),
    ("which tools are you connected to?", False, True),
    ("can you read my Google Drive?", False, True),
    ("do you have access to my calendar?", False, True),
    ("are you connected to Slack?", False, True),
    ("puoi leggere il mio calendario?", False, True),
    ("mi senti?", False, True),
    ("can you hear me?", False, True),
    ("come sei fatta?", False, True),

    # ── genuine public-web questions stay searchable ────────────────────
    ("what's the latest funding round for Anthropic?", False, False),
    ("who won the game last night?", False, False),
]


@pytest.mark.parametrize("text,card,about", CASES, ids=[c[0][:48] for c in CASES])
def test_utterance_routes_as_expected(text: str, card: bool, about: bool) -> None:
    assert engine.wants_action_capture(text) is card, (
        f"capture({text!r}) should be {card}"
    )
    assert engine._is_about_avatar(text) is about, (
        f"about-intent({text!r}) should be {about}"
    )
    if about:
        assert not engine._wants_search(text), (
            f"a self question must never reach the public web: {text!r}"
        )


def test_every_executable_family_is_capturable() -> None:
    """Executable-but-unhearable is the gap this file exists to prevent: for
    each app family the executor can act on, at least one natural phrasing must
    reach the capture seam."""
    phrasings = {
        "calendar": "can you schedule a meeting with Anant tomorrow at 3pm?",
        "email": "send the recap email to daniel@example.com",
        "gmail": "archive the emails from noreply@vendor.example",
        "asana": "create a task in Asana for the Pipedream connection",
        "drive": "can you create a folder in Drive for the Q3 launch?",
    }
    for family, phrase in phrasings.items():
        assert engine.wants_action_capture(phrase), (
            f"{family}: executable but not capturable — {phrase!r}"
        )
