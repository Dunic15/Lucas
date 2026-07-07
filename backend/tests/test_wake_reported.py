"""detect_wake must not fire when the avatar is only being talked ABOUT
(reported speech), while genuine vocatives still wake it. Pure logic."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.avatars import Avatar  # noqa: E402
from app.decision import detect_wake  # noqa: E402


def _avatar(**over) -> Avatar:
    base = dict(
        id="laura",
        name="Laura",
        role="AI Process Expert",
        wake_words=["laura"],
        persona_prompt="",
        anam_avatar_id="r1",
        elevenlabs_voice_id="v1",
        min_confidence=0.55,
        speak_cooldown_seconds=8.0,
        dir=Path("."),
    )
    base.update(over)
    return Avatar(**base)


# ── reported speech must NOT wake ──
def test_as_laura_said_is_not_a_wake():
    called, q = detect_wake(_avatar(), "As Laura said earlier, we should ship Friday.")
    assert called is False
    assert q == ""


def test_what_did_laura_mean_is_not_a_wake():
    called, _ = detect_wake(_avatar(), "What did Laura mean by handoff?")
    assert called is False


def test_laura_mentioned_is_not_a_wake():
    called, _ = detect_wake(_avatar(), "Laura mentioned the deadline last week.")
    assert called is False


# ── genuine vocatives still wake (regression guard) ──
def test_vocative_start_still_wakes():
    called, q = detect_wake(_avatar(), "Laura, what are we missing?")
    assert called is True
    assert q.lower() == "what are we missing"


def test_hey_laura_still_wakes():
    called, _ = detect_wake(_avatar(), "Hey Laura what's the process here?")
    assert called is True


def test_trailing_vocative_still_wakes():
    called, _ = detect_wake(_avatar(), "Can you check that, Laura?")
    assert called is True


def test_vocative_wins_over_reported_mention_in_same_line():
    called, _ = detect_wake(_avatar(), "Laura, what did Laura mean by that?")
    assert called is True
