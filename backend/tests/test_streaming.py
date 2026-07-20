"""Streaming answer contract tests. No network calls or API keys."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine as brain  # noqa: E402
from app.avatars import Avatar  # noqa: E402
from app.rag import Retrieved  # noqa: E402


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


def _retrieved() -> list[Retrieved]:
    return [
        Retrieved(
            text="Provisioning access requires manager approval before IT acts.",
            source="access_security_sop.md",
            section="Provisioning",
            score=0.91,
        )
    ]


def _force_streaming_provider(monkeypatch) -> None:
    monkeypatch.setattr(brain.settings, "brain_provider", "anthropic")
    monkeypatch.setattr(brain.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(brain, "retrieve", lambda *args, **kwargs: _retrieved())


def test_streaming_answer_yields_sentences_and_citation(monkeypatch):
    _force_streaming_provider(monkeypatch)
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *args, **kwargs: iter(
            ["Managers approve access first. ", "Then IT provisions it."]
        ),
    )

    out = list(brain.answer_question_stream(_avatar(), "What is the access flow?"))

    assert out == [
        "Managers approve access first.",
        "Then IT provisions it.",
    ]


def test_streaming_skip_sentinel_stays_silent(monkeypatch):
    _force_streaming_provider(monkeypatch)
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *args, **kwargs: iter(["SK", "IP"]),
    )

    out = list(brain.answer_question_stream(_avatar(), "Unknown policy?"))

    assert out == []


def test_streaming_skip_prefix_inside_word_is_not_sentinel(monkeypatch):
    _force_streaming_provider(monkeypatch)
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *args, **kwargs: iter(["Skipping is not used here."]),
    )

    out = list(brain.answer_question_stream(_avatar(), "What should we avoid?"))

    assert out == ["Skipping is not used here."]


def test_streaming_min_chars_coalesces_tiny_sentences(monkeypatch):
    _force_streaming_provider(monkeypatch)
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *a, **k: iter(["Yes. ", "Sure. ", "The security lead signs off first."]),
    )

    out = list(brain.answer_question_stream(_avatar(), "q", min_chars=20))

    # Tiny fragments are merged into one flowing chunk instead of stuttering.
    assert out == ["Yes. Sure. The security lead signs off first."]
    assert all(len(chunk) >= 20 for chunk in out)


def test_streaming_min_chars_zero_keeps_per_sentence(monkeypatch):
    _force_streaming_provider(monkeypatch)
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *a, **k: iter(["One sentence here. ", "Two sentence there."]),
    )

    out = list(brain.answer_question_stream(_avatar(), "q"))  # min_chars defaults to 0

    assert out == ["One sentence here.", "Two sentence there."]


def test_streaming_retrieval_uses_recent_history(monkeypatch):
    monkeypatch.setattr(brain.settings, "brain_provider", "anthropic")
    monkeypatch.setattr(brain.settings, "anthropic_api_key", "test-key")
    seen = {}

    def fake_retrieve(_avatar, query, *, k):
        seen["query"] = query
        seen["k"] = k
        return _retrieved()

    monkeypatch.setattr(brain, "retrieve", fake_retrieve)
    monkeypatch.setattr(
        brain.llm,
        "stream_complete",
        lambda *args, **kwargs: iter(["Managers approve access."]),
    )

    list(
        brain.answer_question_stream(
            _avatar(),
            "what about that?",
            history="Alice: We need laptop access for the new hire.",
        )
    )

    assert "laptop access" in seen["query"]
    assert "Current ask: what about that?" in seen["query"]
    assert seen["k"] == 6


def test_streaming_search_route_announces_before_answer(monkeypatch):
    _force_streaming_provider(monkeypatch)
    monkeypatch.setattr(brain.settings, "live_search_enabled", True)
    monkeypatch.setattr(
        brain, "_web_search_answer", lambda q, convo="": "The round closed yesterday."
    )

    out = list(
        brain.answer_question_stream(_avatar(), "What is the latest news on the fund?")
    )

    assert out[0] in brain.SEARCH_ANNOUNCE_LINES
    assert "The round closed yesterday." in out[1:]


def test_wants_deep_thought_gates_on_intent_and_key(monkeypatch):
    monkeypatch.setattr(brain.settings, "anthropic_api_key", "test-key")
    assert brain.wants_deep_thought("Should we hire, what are the trade-offs?")
    assert not brain.wants_deep_thought("What is the first onboarding step?")
    # No Anthropic key -> the complex route can't run, so no announce either.
    monkeypatch.setattr(brain.settings, "anthropic_api_key", "")
    assert not brain.wants_deep_thought("Should we hire, what are the trade-offs?")
