"""Honest caveat on ungrounded PROCESS answers
(settings.caveat_ungrounded_process_answers).

When the retrieved chunks are dropped for scoring below rag_min_context_score AND
the question reads as company/process-specific, she PREFACES the answer with a
brief honest caveat instead of presenting world knowledge as if it came from the
docs. A general/world question answers normally, and the grounded (above-floor)
happy path is untouched and pays zero extra latency. No network, no API keys —
the streaming provider is forced and llm.stream_complete is stubbed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine as brain  # noqa: E402
from app.avatars import Avatar  # noqa: E402
from app.config import settings  # noqa: E402
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


def _chunks(score: float) -> list[Retrieved]:
    return [
        Retrieved(
            text="Refunds are processed by finance after manager sign-off.",
            source="refund_policy.md",
            section="Refunds",
            score=score,
        )
    ]


def _force_provider(monkeypatch) -> None:
    # anthropic + a key makes effective_provider() != "stub" so the real
    # streaming path (not _stub_answer) runs; the model call itself is stubbed.
    monkeypatch.setattr(brain.settings, "brain_provider", "anthropic")
    monkeypatch.setattr(brain.settings, "anthropic_api_key", "test-key")


def _stub_model(monkeypatch, text: str) -> None:
    monkeypatch.setattr(
        brain.llm, "stream_complete", lambda *a, **k: iter([text])
    )


# ── the heuristic itself ──


def test_process_specific_heuristic():
    assert brain._looks_process_specific("what's our refund policy?")
    assert brain._looks_process_specific("how does the onboarding process work here?")
    assert brain._looks_process_specific("who approves the SOP?")
    assert brain._looks_process_specific("qual è la nostra policy sui rimborsi?")
    # general / world questions must NOT read as process-specific
    assert not brain._looks_process_specific("what's the capital of France?")
    assert not brain._looks_process_specific("tell me a joke")
    assert not brain._looks_process_specific("how are you doing today?")


# ── streaming behaviour ──


def test_below_floor_process_question_is_caveated(monkeypatch):
    _force_provider(monkeypatch)
    monkeypatch.setattr(settings, "caveat_ungrounded_process_answers", True)
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunks(0.10))
    _stub_model(monkeypatch, "Refunds usually take about 30 days to process.")

    out = list(brain.answer_question_stream(_avatar(), "What's our refund policy?"))

    assert out[0] == brain._CAVEAT_UNGROUNDED_EN  # leads with the honest caveat
    assert any("Refunds" in s for s in out[1:])  # answer still delivered after it


def test_below_floor_general_question_is_not_caveated(monkeypatch):
    _force_provider(monkeypatch)
    monkeypatch.setattr(settings, "caveat_ungrounded_process_answers", True)
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunks(0.10))
    _stub_model(monkeypatch, "Paris is the capital of France.")

    out = list(brain.answer_question_stream(_avatar(), "What's the capital of France?"))

    assert brain._CAVEAT_UNGROUNDED_EN not in out
    assert brain._CAVEAT_UNGROUNDED_IT not in out
    assert out == ["Paris is the capital of France."]


def test_above_floor_grounded_answer_is_unchanged(monkeypatch):
    _force_provider(monkeypatch)
    monkeypatch.setattr(settings, "caveat_ungrounded_process_answers", True)
    # 0.80 is above the floor → chunks survive → genuinely grounded, no caveat.
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunks(0.80))
    _stub_model(monkeypatch, "Finance processes refunds after manager sign-off.")

    out = list(brain.answer_question_stream(_avatar(), "What's our refund policy?"))

    assert brain._CAVEAT_UNGROUNDED_EN not in out
    assert out == ["Finance processes refunds after manager sign-off."]


def test_feature_off_no_caveat(monkeypatch):
    _force_provider(monkeypatch)
    monkeypatch.setattr(settings, "caveat_ungrounded_process_answers", False)
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunks(0.10))
    _stub_model(monkeypatch, "Refunds usually take about 30 days to process.")

    out = list(brain.answer_question_stream(_avatar(), "What's our refund policy?"))

    assert brain._CAVEAT_UNGROUNDED_EN not in out  # exactly today's behaviour
    assert out == ["Refunds usually take about 30 days to process."]


def test_below_floor_process_skip_stays_silent(monkeypatch):
    """A SKIP (speech not directed at her) must stay fully silent — the caveat
    is only ever a lead-in to a real answer, never spoken on its own."""
    _force_provider(monkeypatch)
    monkeypatch.setattr(settings, "caveat_ungrounded_process_answers", True)
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunks(0.10))
    _stub_model(monkeypatch, "SKIP")

    out = list(brain.answer_question_stream(_avatar(), "What's our refund policy?"))

    assert out == []  # no caveat, no answer — silent


def test_italian_below_floor_process_gets_italian_caveat(monkeypatch):
    _force_provider(monkeypatch)
    monkeypatch.setattr(settings, "caveat_ungrounded_process_answers", True)
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunks(0.10))
    _stub_model(monkeypatch, "I rimborsi richiedono l'approvazione del manager.")

    # Both process-specific (nostra/procedura/rimborso) AND clearly Italian
    # (puoi + è) so the caveat is picked in Italian.
    out = list(
        brain.answer_question_stream(
            _avatar(), "puoi dirmi qual è la nostra procedura di rimborso?"
        )
    )

    assert out[0] == brain._CAVEAT_UNGROUNDED_IT
