"""Answer policy: the live meeting path uses the stricter system prompt and a
mechanical 1-2 sentence cap (not prompt-only). No network / keys."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import brain  # noqa: E402
from app.avatars import Avatar  # noqa: E402
from app.rag import Retrieved  # noqa: E402


def _avatar() -> Avatar:
    return Avatar(
        id="laura",
        name="Laura",
        role="AI Process Expert",
        wake_words=["laura"],
        persona_prompt="PERSONA",
        anam_avatar_id="r1",
        elevenlabs_voice_id="v1",
        min_confidence=0.55,
        speak_cooldown_seconds=8.0,
        dir=Path("."),
    )


def _force(monkeypatch):
    monkeypatch.setattr(brain.settings, "brain_provider", "anthropic")
    monkeypatch.setattr(brain.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(
        brain,
        "retrieve",
        lambda *a, **k: [Retrieved(text="ctx", source="sop.md", section="s", score=0.9)],
    )


def test_max_sentences_caps_spoken_output(monkeypatch):
    _force(monkeypatch)
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *a, **k: iter(["One. ", "Two. ", "Three. ", "Four."]),
    )
    out = list(
        brain.answer_question_stream(_avatar(), "q", live=True, max_sentences=2)
    )
    assert out == ["One.", "Two."]


def test_live_mode_uses_meeting_system_prompt(monkeypatch):
    _force(monkeypatch)
    captured = {}

    def fake_stream(system, user, **kwargs):
        captured["system"] = system
        return iter(["Answer."])

    monkeypatch.setattr(brain.llm, "stream_complete", fake_stream)
    list(brain.answer_question_stream(_avatar(), "q", live=True, max_sentences=2))
    # The meeting prompt is the silence-biased one and carries the persona.
    assert "You are NOT the host" in captured["system"]
    assert "PERSONA" in captured["system"]


def test_min_chars_coalesces_tiny_sentences(monkeypatch):
    _force(monkeypatch)
    # Three tiny fragments that individually would stutter the TTS voice.
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *a, **k: iter(["Yes. ", "Sure. ", "The security lead signs off first."]),
    )
    out = list(brain.answer_question_stream(_avatar(), "q", live=True, min_chars=20))
    # "Yes. Sure." is only 10 chars -> keeps buffering; flushes once it crosses 20.
    assert out == ["Yes. Sure. The security lead signs off first."]
    assert all(len(chunk) >= 20 for chunk in out)


def test_min_chars_zero_keeps_per_sentence_streaming(monkeypatch):
    _force(monkeypatch)
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *a, **k: iter(["One sentence here. ", "Two sentence there."]),
    )
    out = list(brain.answer_question_stream(_avatar(), "q"))  # min_chars defaults to 0
    assert out == ["One sentence here.", "Two sentence there."]


def test_min_chars_still_respects_max_sentences(monkeypatch):
    _force(monkeypatch)
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *a, **k: iter(["A. ", "B. ", "C. ", "D."]),
    )
    # Cap at 2 sentences even while coalescing: only "A." and "B." get through.
    out = list(
        brain.answer_question_stream(_avatar(), "q", live=True, min_chars=50, max_sentences=2)
    )
    assert out == ["A. B."]


def test_demo_mode_uses_friendly_system_prompt(monkeypatch):
    _force(monkeypatch)
    captured = {}

    def fake_stream(system, user, **kwargs):
        captured["system"] = system
        return iter(["Answer."])

    monkeypatch.setattr(brain.llm, "stream_complete", fake_stream)
    list(brain.answer_question_stream(_avatar(), "q"))  # live defaults to False
    assert "You are NOT the host" not in captured["system"]
    assert "warm, helpful AI assistant" in captured["system"]
