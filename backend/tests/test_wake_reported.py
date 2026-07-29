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


# ── third-party name-sake (the 2026-07-29 "other Cedric" incident) ──
# The room discussed a DIFFERENT person named Cedric who was about to join;
# the avatar must read every such line as talk ABOUT someone, never a wake.
def _cedric(**over):
    return _avatar(id="cedric", name="Cedric", wake_words=["cedric"], **over)


def test_the_other_cedric_is_not_a_wake():
    called, _ = detect_wake(_cedric(), "The other Cedric will join the call.")
    assert called is False


def test_another_cedric_is_not_a_wake():
    called, _ = detect_wake(_cedric(), "There's another Cedric on the team.")
    assert called is False


def test_cedric_is_joining_is_not_a_wake():
    called, _ = detect_wake(_cedric(), "Cedric is joining in five minutes.")
    assert called is False


def test_cedric_will_handle_is_not_a_wake():
    called, _ = detect_wake(_cedric(), "Cedric will handle the rollout next week.")
    assert called is False


def test_comma_dropped_second_person_still_wakes():
    # Live ASR loses the vocative comma: "Cedric is there…" must still wake
    # when the continuation is unmistakably second-person.
    called, q = detect_wake(_cedric(), "Cedric is there a way to fix this?")
    assert called is True
    assert "way to fix this" in q.lower()


def test_cedric_can_you_still_wakes():
    called, _ = detect_wake(_cedric(), "Cedric can you check the pipeline?")
    assert called is True


# ── roster collision: a HUMAN shares the wake word ──
# With a real Cedric in the room every bare mention is ambiguous, so only a
# clear vocative wakes the avatar.
def test_bare_mention_with_human_namesake_does_not_wake():
    # "loop in Cedric" is neither reported speech nor a vocative — without a
    # name-sake it wakes (exact token); with a human Cedric present it must not.
    called, _ = detect_wake(
        _cedric(),
        "Let's loop in Cedric on the pricing question.",
        exclude_names=["Cedric Dupont"],
    )
    assert called is False


def test_vocative_with_human_namesake_still_wakes():
    called, q = detect_wake(
        _cedric(),
        "Hey Cedric, what does the onboarding SOP say?",
        exclude_names=["Cedric Dupont"],
    )
    assert called is True
    assert "onboarding" in q.lower()


def test_no_collision_bare_mention_unchanged():
    # Without a human name-sake the historical behavior stands: an exact token
    # in a groundable line wakes (subject to the reported-speech filter).
    called, _ = detect_wake(_cedric(), "Let's loop in Cedric on the pricing question.")
    assert called is True
