"""Pure-logic tests for the when-to-speak gate. No API keys needed.

Run:  pytest backend/tests -q   (pip install pytest first)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.avatars import Avatar  # noqa: E402
from app.decision import detect_wake, passes_confidence  # noqa: E402


def _avatar(**over) -> Avatar:
    base = dict(
        id="lucas",
        name="Lucas",
        role="AI Process Expert",
        wake_words=["lucas"],
        persona_prompt="",
        anam_avatar_id="r1",
        elevenlabs_voice_id="v1",
        min_confidence=0.55,
        speak_cooldown_seconds=8.0,
        dir=Path("."),
    )
    base.update(over)
    return Avatar(**base)


def test_wake_triggers_and_strips_name():
    called, q = detect_wake(_avatar(), "Lucas, what are we missing?")
    assert called is True
    assert q.lower() == "what are we missing"


def test_no_wake_word_stays_silent():
    called, q = detect_wake(_avatar(), "What is the onboarding process?")
    assert called is False
    assert q == ""


def test_wake_word_must_be_whole_token():
    # "lucaslike" should NOT trigger the "lucas" wake word.
    called, _ = detect_wake(_avatar(), "this is lucaslike behaviour")
    assert called is False


def test_custom_wake_words():
    called, _ = detect_wake(_avatar(wake_words=["marcus", "it expert"]), "hey Marcus?")
    assert called is True


def test_confidence_gate():
    a = _avatar(min_confidence=0.6)
    assert passes_confidence(a, {"sufficient_context": True, "confidence": 0.7})
    assert not passes_confidence(a, {"sufficient_context": True, "confidence": 0.5})
    assert not passes_confidence(a, {"sufficient_context": False, "confidence": 0.9})
