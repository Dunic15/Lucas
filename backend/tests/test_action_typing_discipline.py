"""Typing discipline (owner 2026-07-24: "non tornare sempre ad Asana").

Live evidence: prod queued_actions held approved Asana tasks for "Search the
internet for the Y Combinator application deadline" and "How would you organize
who does what?" — a web lookup and a QUESTION, filed onto the team's real board
by the old "every leftover becomes an asana.create_task" default.

Two rules, enforced across the stub and LLM typing paths:
1. read-vs-write — questions / info-requests are answered in the meeting and
   are never typed into executable cards;
2. Asana is opt-in per item — only an explicit task/ticket/Asana/board cue
   types asana.create_task; everything else stays untyped for the human.
Key-free — no vendors.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine  # noqa: E402


# ── rule 1: questions / info-requests never become executable cards ─────
_READ_ITEMS = [
    "Search the internet for the Y Combinator application deadline",
    "How would you organize who does what?",
    "What's the deadline for the YC application?",
    "Check what is on my calendar tomorrow",
    "Look up the latest funding round for Acme",
    "cerca su internet le ultime notizie sul fondo",
]


@pytest.mark.parametrize("item", _READ_ITEMS)
def test_info_requests_are_not_typeable(item):
    assert engine._typeable(item) is False, item


@pytest.mark.parametrize("item", _READ_ITEMS)
def test_info_requests_stay_untyped_even_with_asana(item):
    out = engine.type_actions([{"item": item}], provider="stub",
                              allow_asana=True)
    assert "typed" not in out[0], item


def test_polite_requests_are_still_typeable():
    """The interrogative gate must NOT swallow polite imperatives — live
    captures are stored as the raw ask ("Can you send…")."""
    for item in ("Can you send the recap to marco@acme.com",
                 "Could you schedule a follow-up with Marco",
                 "Send the recap to marco@acme.com"):
        assert engine._typeable(item) is True, item
    out = engine.type_actions(
        [{"item": "Can you send the recap to marco@acme.com"}],
        provider="stub",
    )
    assert out[0]["typed"]["type"] == "email.send"


# ── rule 2: Asana only on an explicit cue ────────────────────────────────
def test_generic_work_items_stay_untyped():
    """No task/Asana cue → untyped, even with allow_asana (the human types it
    at the approve door if they want it filed)."""
    for item in ("Share the roadmap deck with the team",
                 "Follow up with the vendor about pricing",
                 "Update the onboarding doc before Friday"):
        out = engine.type_actions([{"item": item}], provider="stub",
                                  allow_asana=True)
        assert "typed" not in out[0], item


def test_explicit_task_cues_still_type_asana():
    for item in ("Create an Asana task for the onboarding checklist",
                 "Add a task to the board for the Q3 review",
                 "Open a ticket for the login bug"):
        out = engine.type_actions([{"item": item}], provider="stub",
                                  allow_asana=True)
        assert out[0].get("typed", {}).get("type") == "asana.create_task", item


def test_email_and_calendar_families_unaffected():
    out = engine.type_actions(
        [{"item": "Send the recap to dana@acme.com"},
         {"item": "Schedule a call. 2026-08-01T15:00:00 2026-08-01T16:00:00"}],
        provider="stub", allow_asana=True,
    )
    assert out[0]["typed"]["type"] == "email.send"
    assert out[1]["typed"]["type"] == "calendar.create_event"


# ── the prompts carry the same discipline (LLM path) ─────────────────────
def test_asana_prompt_is_opt_in_not_catch_all():
    p = engine.TYPED_ACTION_ASANA
    assert "every OTHER item" not in p          # the old catch-all is gone
    assert "ONLY when the item explicitly" in p
    assert "stays untyped" in p


def test_extraction_prompt_excludes_info_requests():
    from app.brain.engine import POSTMEETING_SYSTEM
    assert "INFORMATION REQUEST" in POSTMEETING_SYSTEM
    assert "must NEVER appear in actions[]" in POSTMEETING_SYSTEM
