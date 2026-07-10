"""Live repair response tests. No API calls or secrets needed."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402
from app.avatars import Avatar  # noqa: E402
from app import store  # noqa: E402


def _avatar() -> Avatar:
    return Avatar(
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


def test_repair_triggers_when_laura_was_called():
    assert main._should_repair_silent_answer(True, "no no it does not work")


def test_repair_triggers_for_live_check_in_phrases():
    assert main._should_repair_silent_answer(False, "can you hear me?")
    assert main._should_repair_silent_answer(False, "it doesn't work")


def test_repair_line_prompts_for_a_clear_question():
    line = main._silent_answer_repair_line(_avatar())

    assert "I can hear you" in line
    # generic invitation (knowledge packs change; the line must not hardcode
    # doc titles) + a concrete example she can actually answer
    assert "processes" in line and "portfolio" in line
    assert "Laura," in line


def test_avatar_speech_queues_when_websocket_is_missing():
    session = store.Session(bot_id="bot_test", meeting_url="https://meet.test")
    object.__setattr__(session, "_persist_enabled", False)

    asyncio.run(main._make_avatar_speak(session, "Hello from Laura."))

    assert session.ws is None
    assert store.drain_avatar_messages(session) == [
        {
            "type": "speak",
            "text": "Hello from Laura.",
            "citations": [],
            "generation_id": 0,
            "emotion": "neutral",  # per-sentence emotion rides every speak
        }
    ]
