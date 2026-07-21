"""answer_question grounding floor: on weak retrieval the metadata must not
falsely claim the answer is document-grounded (sufficient_context + citations).
The model self-reports sufficient_context and sometimes labels a world-knowledge
answer as grounded; a retrieval-score floor corrects the metadata deterministically
(the answer text is left alone — Laura is a general assistant first). Key-free/stub.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars  # noqa: E402
from app.brain import engine as brain  # noqa: E402
from app.config import settings  # noqa: E402
from app.rag import Retrieved  # noqa: E402


def _chunks(score: float) -> list[Retrieved]:
    return [
        Retrieved(
            text="Onboarding takes two weeks with a security review included.",
            source="onboarding_sop.md",
            section="Onboarding",
            score=score,
        )
    ]


def test_weak_retrieval_forces_ungrounded_metadata(monkeypatch):
    # 0.30 clears the stub's 0.12 answer threshold (so the pre-floor result
    # reports sufficient_context=True + a citation) but is below the 0.45 floor:
    # the answer did NOT come from the docs, so the metadata must say so.
    monkeypatch.setattr(brain, "_is_stub", lambda: True)
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunks(0.30))
    r = brain.answer_question(avatars.load("laura"), "Who won the 2022 World Cup?")
    assert r["sufficient_context"] is False
    assert r["citations"] == []


def test_strong_retrieval_keeps_grounded_metadata(monkeypatch):
    # A real document match (0.74) stays grounded with its citation intact.
    monkeypatch.setattr(brain, "_is_stub", lambda: True)
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunks(0.74))
    r = brain.answer_question(avatars.load("laura"), "How long is onboarding?")
    assert r["sufficient_context"] is True
    assert r["citations"] == ["onboarding_sop.md"]


def test_floor_is_configurable(monkeypatch):
    # Lower the floor below the score → 0.30 now counts as grounded (env-tunable).
    monkeypatch.setattr(brain, "_is_stub", lambda: True)
    monkeypatch.setattr(brain, "_retrieve_for", lambda *a, **k: _chunks(0.30))
    monkeypatch.setattr(settings, "answer_grounding_floor", 0.20)
    r = brain.answer_question(avatars.load("laura"), "anything")
    assert r["sufficient_context"] is True
