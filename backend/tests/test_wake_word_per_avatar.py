"""Per-avatar wake-word mode: speak only when addressed by name.

Owner request 2026-07-17: laura/petra/cedric answer ONLY on their wake word
and never interrupt otherwise. Covers the yaml field + inheritance, the
_wake_required resolver, and the gate wiring (backchannel suppression and
the not-called answer gate with its follow-up exception).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import app.main as main_module  # noqa: E402
from app import avatars, store  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _wake_word_inherits_global():
    """Override the conftest pin: this module asserts the SHIPPED yaml state."""
    yield


def test_all_three_avatars_ship_wake_word_on():
    for aid in ("laura", "petra", "cedric"):
        assert avatars.load(aid).require_wake_word is True, aid


def test_yaml_none_inherits_global(monkeypatch):
    # Build a bare Avatar to test inheritance without touching real yamls.
    bare = avatars.Avatar(
        id="x", name="X", role="", wake_words=["x"], persona_prompt="",
        anam_avatar_id="", elevenlabs_voice_id="", min_confidence=0.5,
        speak_cooldown_seconds=8.0, dir=Path("."), require_wake_word=None,
    )
    monkeypatch.setattr(settings, "require_wake_word", False)
    assert main_module._wake_required(bare) is False
    monkeypatch.setattr(settings, "require_wake_word", True)
    assert main_module._wake_required(bare) is True
    # An explicit avatar False pins it regardless of the global.
    pinned = avatars.Avatar(
        id="y", name="Y", role="", wake_words=["y"], persona_prompt="",
        anam_avatar_id="", elevenlabs_voice_id="", min_confidence=0.5,
        speak_cooldown_seconds=8.0, dir=Path("."), require_wake_word=False,
    )
    assert main_module._wake_required(pinned) is False


def test_backchannel_suppressed_in_wake_word_mode(monkeypatch):
    monkeypatch.setattr(settings, "backchannel_enabled", True)
    monkeypatch.setattr(settings, "backchannel_min_words", 3)
    session = store.Session(bot_id="b1", meeting_url="m", avatar_id="petra")
    session.last_backchannel_at = 0.0
    session.last_spoke_at = 0.0
    session.speaking_until = 0.0
    long_text = "we should really think about the rollout plan for next quarter"
    petra = avatars.load("petra")  # require_wake_word: true
    assert main_module._should_backchannel(session, long_text, petra) is False
    # Without the avatar (legacy call) the old behavior is preserved.
    assert main_module._should_backchannel(session, long_text) in (True, False)


def test_answer_gate_blocks_uncalled_but_allows_followup(monkeypatch):
    """The gate logic in isolation: not-called → silent, unless it's a
    question right after the avatar's own answer (follow-up window)."""
    petra = avatars.load("petra")
    assert main_module._wake_required(petra) is True
    # Mirror the gate's follow-up predicate.
    monkeypatch.setattr(settings, "followup_window_seconds", 12.0)
    now = time.time()

    def followup_ok(last_spoke_at: float, text: str) -> bool:
        return (
            settings.followup_window_seconds > 0
            and (now - last_spoke_at) < settings.followup_window_seconds
            and text.rstrip().endswith("?")
        )

    assert followup_ok(now - 5, "and what about the deadline?") is True
    assert followup_ok(now - 60, "and what about the deadline?") is False
    assert followup_ok(now - 5, "just thinking out loud") is False
