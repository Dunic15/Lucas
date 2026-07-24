"""Clarify detail-matchers must recognize how people actually answer out loud.

Live repro 2026-07-23 (bot 760f9af8): asked to schedule a meeting, Petra asked
"who should be on it, and when?" — Ananth answered "me and duccio at SFF studio
dot com" and "tomorrow at five PM", and she re-asked the identical question 4×.
Cause: `_DETAIL_INVITE_WITH` only matched "with X"/"between me and X" (not
"me and X"), and `_DETAIL_INVITE_CLOCK` only matched digit times (not "five PM"/
"seven p m"), so the answers never filled their slots.

Key-free unit tests on the matchers + the full transcript flow.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import tools  # noqa: E402


def _missing(text: str) -> list[str]:
    return tools.missing_action_details(text, kind="calendar")


# ── the exact transcript flow now resolves ──

def test_transcript_flow_resolves_and_stops_asking():
    ask = "can you schedule a meeting, on Google Calendar, please?"
    # She asks for what's missing.
    assert _missing(ask)  # not empty → clarify fires
    # Attendee answer fills invite_with.
    after_who = ask + " It should be me and duccio at SFF studio dot com."
    assert "invite_with" not in _missing(after_who)
    # Time answer fills invite_when → nothing left, no more re-asking.
    after_when = after_who + " And it should be for tomorrow at five PM."
    assert _missing(after_when) == []


def test_seven_pm_spoken_as_p_m_fills_time():
    ask = ("schedule a meeting with duccio "
           "tomorrow at seven p m.")
    assert _missing(ask) == []


# ── attendee phrasings ──

@pytest.mark.parametrize("who", [
    "me and duccio",
    "duccio and me",
    "duccio@sffstudio.com",
    "duccio at sff studio dot com",
    "with the team",
    "between me and marco",
])
def test_attendee_phrasings_recognized(who):
    from app.brain.tools import _DETAIL_INVITE_WITH
    assert _DETAIL_INVITE_WITH.search(who), who


# ── time phrasings ──

@pytest.mark.parametrize("when", [
    "at 3pm", "3:30 pm", "at five PM", "five pm", "seven p m",
    "seven o'clock", "at noon", "at midnight",
])
def test_clock_phrasings_recognized(when):
    from app.brain.tools import _DETAIL_INVITE_CLOCK
    assert _DETAIL_INVITE_CLOCK.search(when), when


# ── false-positive guard: an underspecified ask must STILL clarify ──

@pytest.mark.parametrize("bare", [
    "schedule a meeting on Google Calendar please",
    "book a meeting",
    "can you set up a call",
])
def test_bare_ask_still_prompts(bare):
    # At least one slot must remain missing so clarification still fires.
    assert _missing(bare), bare


def test_date_without_time_still_incomplete():
    # A day alone is not a scheduleable time — but the follow-up now narrows
    # to the missing HALF (invite_clock = "what time") instead of re-asking
    # the identical "when it should be?" (live 2026-07-24).
    assert "invite_clock" in _missing("schedule a meeting with duccio tomorrow")
