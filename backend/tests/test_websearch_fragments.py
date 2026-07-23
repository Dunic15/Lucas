"""Web search must not fire on broken fragments or internal-workspace asks.

Live 2026-07-23: the ASR fragment "No. I'm not online. Not online. Just…" —
a user correcting themselves mid-sentence — triggered "let me look that up
online" because bare "online"/"web" were sufficient search triggers. They now
only count inside an explicit search phrase.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine
from app.config import settings


@pytest.fixture(autouse=True)
def _search_on(monkeypatch):
    # _wants_search is gated on the feature flag + an Anthropic key; force them
    # on so the test exercises the INTENT logic, not the config gate.
    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")


# (utterance, should_web_search)
CASES = [
    # ── must NOT search: the live bug + neighbours ──────────────────────
    ("No. I'm not online. Not online. Just", False),
    ("I'm not online, I mean local", False),
    ("we're not on the web here", False),
    ("can you read Northwind Labs?", False),      # internal docs
    ("what does the process document say?", False),
    ("what's in my Asana dashboard?", False),
    ("are you connected to the internet?", False),  # a self/capability question

    # ── must STILL search: real fresh-info asks ─────────────────────────
    ("what's the latest funding round for Anthropic?", True),
    ("search online for the best CRM for startups", True),
    ("look it up on the web", True),
    ("who won the game last night?", True),
    ("what's the news today?", True),
    ("google the weather in Milan", True),
    ("what happened in the markets today?", True),
]


@pytest.mark.parametrize("text,want", CASES, ids=[c[0][:44] for c in CASES])
def test_web_search_intent(text: str, want: bool) -> None:
    assert engine._wants_search(text) is want, (
        f"_wants_search({text!r}) should be {want}"
    )


def test_bare_online_no_longer_triggers_but_the_phrase_does():
    assert engine._wants_search("not online anyway") is False
    assert engine._wants_search("search online for it") is True
