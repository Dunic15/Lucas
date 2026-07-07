"""When-to-speak policy: reported-speech false wakes, direct on-topic questions,
and the near-duplicate similarity used by the repetition guard. Pure logic — no
API keys, no network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.avatars import Avatar  # noqa: E402
from app.decision import (  # noqa: E402
    detect_wake,
    is_direct_question,
    is_near_duplicate,
    similarity,
)


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
        topics_hint="onboarding",
    )
    base.update(over)
    return Avatar(**base)


# ── reported speech must NOT wake the avatar ──
def test_reported_speech_as_laura_said_is_not_a_wake():
    called, q = detect_wake(_avatar(), "As Laura said earlier, we should ship Friday.")
    assert called is False
    assert q == ""


def test_reported_speech_what_did_laura_mean_is_not_a_wake():
    called, _ = detect_wake(_avatar(), "What did Laura mean by handoff?")
    assert called is False


def test_reported_speech_laura_mentioned_is_not_a_wake():
    called, _ = detect_wake(_avatar(), "Laura mentioned the deadline last week.")
    assert called is False


# ── genuine vocatives still wake, even alongside a third-person mention ──
def test_vocative_still_wakes():
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


# ── direct on-topic questions (no wake word) ──
def test_direct_process_question_without_wake_word():
    assert is_direct_question(_avatar(), "What approval step is required before go-live?")


def test_direct_sff_question_without_wake_word():
    sff = _avatar(id="sff", wake_words=["laura", "sff"])
    assert is_direct_question(sff, "Which companies are in the portfolio?")


def test_offtopic_question_is_not_direct():
    assert not is_direct_question(_avatar(), "Did anyone watch the game last night?")


def test_statement_is_not_a_direct_question():
    assert not is_direct_question(_avatar(), "The approval process is done.")


def test_reported_speech_question_is_not_direct():
    # Talking about her, even with a "?" and a domain word, must not count.
    assert not is_direct_question(_avatar(), "What did Laura say about approval?")


# ── near-duplicate similarity (repetition guard core) ──
def test_similarity_identical_is_one():
    assert similarity("what is the approval process", "what is the approval process") == 1.0


def test_near_duplicate_detects_reworded_same_ask():
    priors = ["what is the security approval process"]
    assert is_near_duplicate("remind me the security approval process", priors)


def test_near_duplicate_rejects_different_ask():
    priors = ["what is the security approval process"]
    assert not is_near_duplicate("when does the customer go live", priors)
