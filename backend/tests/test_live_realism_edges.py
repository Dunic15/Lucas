"""Adversarial edge coverage for the pure live-conversation decision seams."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import decision, emotion, end_of_turn  # noqa: E402
from app.avatars import Avatar  # noqa: E402


def _avatar() -> Avatar:
    return Avatar(
        id="laura",
        name="Laura",
        role="AI Process Expert",
        wake_words=["laura"],
        persona_prompt="",
        anam_avatar_id="",
        elevenlabs_voice_id="",
        min_confidence=0.6,
        speak_cooldown_seconds=8.0,
        dir=Path("."),
    )


def test_empty_input_is_silent_neutral_and_holds_floor():
    avatar = _avatar()

    assert end_of_turn.completeness("") == 0.0
    assert decision.detect_wake(avatar, "") == (False, "")
    assert not decision.addressed_to_other("", ["Marco Bell"])
    assert not decision.detect_closing("")
    assert not decision.detect_stop_command("")
    assert not decision.detect_leave_command("")
    assert not decision.fuzzy_name_match("", "laura")
    assert not decision.passes_confidence(avatar, {})
    assert emotion.classify("") == "neutral"
    assert emotion.talk_mood(None) == "neutral"
    assert emotion.ditto_emo(None) == 4


@pytest.mark.parametrize(
    "utterance",
    [
        "Laura cosa ne pensi",
        "Laura what do you think",
        "Lora puoi controllare",
    ],
)
def test_wake_detection_survives_bilingual_asr_without_punctuation(utterance: str):
    called, question = decision.detect_wake(_avatar(), utterance)

    assert called
    assert question


def test_control_intents_survive_bilingual_asr_without_punctuation():
    assert decision.detect_stop_command("aspetta")
    assert decision.detect_stop_command("hold on")
    assert decision.detect_leave_command("puoi uscire dalla riunione")
    assert decision.detect_leave_command("please leave the meeting")
    assert decision.detect_closing("prima di chiudere un ultima cosa")
    assert decision.detect_closing("anything else before we wrap up")


def test_numbers_do_not_create_false_names_or_control_intents():
    avatar = _avatar()

    assert end_of_turn.completeness("What is item 42?") >= 0.8
    assert not decision.fuzzy_name_match("42", "laura")
    assert not decision.detect_stop_command("stop after 42 seconds")
    assert not decision.detect_leave_command("leave 42 minutes for questions")
    assert decision.passes_confidence(
        avatar, {"sufficient_context": True, "confidence": "0.60"}
    )
    assert not decision.passes_confidence(
        avatar, {"sufficient_context": True, "confidence": 0.59}
    )


def test_emoji_keeps_emotion_precedence_and_safe_renderer_mappings():
    concerned = emotion.classify("Great job, but this is at risk 😬")
    excited = emotion.classify("Complimenti a tutti! 🎉")

    assert concerned == "concerned"
    assert emotion.talk_mood(concerned) == "sad"
    assert emotion.ditto_emo(concerned) == 5
    assert excited == "excited"
    assert emotion.talk_mood(excited) == "happy"
    assert emotion.ditto_emo(excited) == 3


@pytest.mark.xfail(
    reason="addressed_to_other misses ASR vocatives when the comma is absent"
)
def test_asr_vocative_without_punctuation_defers_to_named_human():
    assert decision.addressed_to_other(
        "Marco can you confirm the total", ["Lian Park", "Marco Bell"]
    )


@pytest.mark.xfail(
    reason="end_of_turn.completeness treats a mid-word ASR cutoff as yielded"
)
def test_mid_word_asr_cutoff_holds_the_floor():
    assert end_of_turn.completeness("I also wanted to ask abou") <= 0.3


@pytest.mark.xfail(
    reason="trailing incomplete-word precedence overrides a terminal question mark"
)
def test_completed_question_ending_in_that_yields_the_floor():
    assert end_of_turn.completeness("What did Laura mean by that?") >= 0.8


@pytest.mark.xfail(
    reason="terminal emoji hides the sentence punctuation from completeness"
)
def test_terminal_emoji_preserves_completed_punctuation():
    assert end_of_turn.completeness("Great job! 🎉") >= 0.8


@pytest.mark.xfail(reason="app.decision.detect_invite is not implemented on main")
def test_explicit_invitation_to_speak_is_detected():
    detector = getattr(decision, "detect_invite", None)

    assert detector is not None
    assert detector("would you like to add anything")
