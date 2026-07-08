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


def test_wake_triggers_and_strips_name():
    called, q = detect_wake(_avatar(), "Laura, what are we missing?")
    assert called is True
    assert q.lower() == "what are we missing"


def test_no_wake_word_stays_silent():
    called, q = detect_wake(_avatar(), "What is the onboarding process?")
    assert called is False
    assert q == ""


def test_wake_word_must_be_whole_token():
    # "lauralike" should NOT trigger the "laura" wake word.
    called, _ = detect_wake(_avatar(), "this is lauralike behaviour")
    assert called is False


def test_custom_wake_words():
    called, _ = detect_wake(_avatar(wake_words=["marcus", "it expert"]), "hey Marcus?")
    assert called is True


def test_confidence_gate():
    a = _avatar(min_confidence=0.6)
    assert passes_confidence(a, {"sufficient_context": True, "confidence": 0.7})
    assert not passes_confidence(a, {"sufficient_context": True, "confidence": 0.5})
    assert not passes_confidence(a, {"sufficient_context": False, "confidence": 0.9})


# ── stop command ("Laura, stop / aspetta") ──
from app.decision import detect_stop_command  # noqa: E402

STOP_ASKS = [
    "stop",
    "wait",
    "hold on",
    "one sec",
    "okay stop please",
    "never mind",
    "stop talking",
    "that's enough",
    "aspetta",
    "fermati",
    "un attimo per favore",
    "basta così",
    "zitta",
    "lascia stare",
]

NOT_STOP_ASKS = [
    "stop the deploy",
    "when do we stop the meter",
    "wait for the client to confirm",
    "can you pause the recording",
    "aspetta il cliente prima di mandare la mail",
    "basta parlare del budget, passiamo oltre",
    "what happens if we stop paying",
]


def test_stop_commands_detected():
    for ask in STOP_ASKS:
        assert detect_stop_command(ask), f"should stop: {ask!r}"


def test_stop_not_triggered_by_normal_talk():
    for ask in NOT_STOP_ASKS:
        assert not detect_stop_command(ask), f"must NOT stop: {ask!r}"


def test_italian_closing_detected():
    from app.decision import detect_closing
    for line in (
        "per riassumere, direi che ci siamo",
        "prima di chiudere, un'ultima cosa",
        "abbiamo finito per oggi",
        "qualcos'altro da discutere?",
        "ci aggiorniamo la prossima settimana",
    ):
        assert detect_closing(line), line


def test_italian_reported_speech_does_not_wake():
    from app.avatars import Avatar
    from app.decision import detect_wake
    from pathlib import Path
    a = Avatar(id="laura", name="Laura", role="x", wake_words=["laura"],
               persona_prompt="", anam_avatar_id="", elevenlabs_voice_id="",
               min_confidence=0.5, speak_cooldown_seconds=8.0, dir=Path("."))
    for line in (
        "come ha detto Laura, il DPA manca",
        "secondo Laura dovremmo aspettare",
        "Laura ha detto che il deadline è venerdì",
        "Laura diceva una cosa simile",
    ):
        called, _ = detect_wake(a, line)
        assert not called, line
    # vocative still wakes
    called, q = detect_wake(a, "Laura, cosa ne pensi?")
    assert called and q
