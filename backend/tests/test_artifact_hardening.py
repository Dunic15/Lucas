"""Post-meeting artifact never ships a garbled recap, even when the model emits
malformed JSON or echoes transcript chunks into a list field. Deterministic —
no model, no keys."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import meeting_state
from app.brain import engine as brain


class _FakeState:
    """Minimal MeetingState stand-in with clean tracked data."""
    decisions = [{"decision": "Ship v1 on Monday"}]
    risks = [{"risk": "Security review pending"}]
    missing_steps: list = []
    meeting_type = "status_update"
    required_steps: list = []
    completed_steps: list = []
    per_person: dict = {}

    def readiness_score(self):
        return 80


def test_clean_lines_drops_transcript_shaped_entries():
    transcript = "Duccio: hi. Marco: I will do it. Elena: sounds good to me and more"
    kept = brain._clean_lines(
        ["Ship on Monday", transcript, "x" * 300, "", "Pricing is monthly"]
    )
    assert kept == ["Ship on Monday", "Pricing is monthly"]  # clean lines only
    assert brain._clean_lines("not a list") == []  # non-list is safe
    assert brain._clean_lines(None) == []


def test_degraded_artifact_is_rebuilt_from_state():
    # Parse-failure fallback shape: {"answer": <raw>} with no summary/actions.
    degraded = {"answer": "Duccio: ... Marco: ...", "confidence": 0.0}
    out = brain._finish_artifact(degraded, _FakeState())
    # The junk 'answer' text never becomes summary/decisions.
    assert out["summary"] == ""
    assert out["decisions"] == ["Ship v1 on Monday"]  # from state
    assert out["risks"] == ["Security review pending"]
    assert out["actions"] == []
    assert "answer" not in out or not out.get("summary")


def test_transcript_echoed_into_decisions_is_filtered():
    # Model returned valid JSON but dumped the transcript as one 'decision'.
    art = {
        "summary": "We discussed the pilot.",
        "decisions": [
            "Duccio: Pilot July 15. Marco: rollout doc Friday. Elena: pricing monthly.",
            "Pricing is monthly",
        ],
        "actions": [{"item": "Send doc", "owner": "Marco"}],
    }
    out = brain._finish_artifact(art, _FakeState())
    # The transcript-shaped decision is dropped; the real one survives.
    assert out["decisions"] == ["Pricing is monthly"]
    assert out["summary"] == "We discussed the pilot."  # good summary kept
    assert out["actions"] == [{"item": "Send doc", "owner": "Marco"}]


def test_post_meeting_degraded_model_yields_real_summary(monkeypatch):
    """The empty-summary bug: when the post model returns unusable output
    (truncated/non-JSON), post_meeting must rebuild a REAL recap from the
    deterministic tracker — a non-empty summary + email — never a blank artifact.
    Before the fix this shipped summary "" with participation intact."""
    from app import avatars
    from app.config import settings

    # Force the non-stub post path, then make the model return prose (not JSON),
    # so _parse_json degrades to {"answer": ...}.
    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(
        brain.llm, "complete", lambda *a, **k: "Sorry — I couldn't format that as JSON."
    )
    monkeypatch.setattr(brain, "retrieve", lambda avatar, q, k=6: [])

    avatar = avatars.load("laura")
    transcript = (
        "Ben: Welcome everyone, quick sync.\n"
        "Ben: Please send the recap to Marco by Friday.\n"
        "Priya: We should schedule a follow-up next week.\n"
    )
    art = brain.post_meeting(avatar, transcript)

    assert art["summary"].strip()  # NEVER empty — this is the whole point
    assert "incomplete result" in art["summary"]  # degrade mode note, not stub note
    assert art["follow_up_email"].get("body")  # email is populated too
    # Participation still comes from the tracker (unchanged behaviour).
    assert isinstance(art["participation"], list)


def test_good_artifact_passes_through():
    art = {
        "summary": "Clean summary.",
        "decisions": ["Go monthly", "Launch Monday"],
        "risks": ["Legal review open"],
        "actions": [{"item": "Book review", "owner": "Ben"}],
    }
    out = brain._finish_artifact(art, _FakeState())
    assert out["decisions"] == ["Go monthly", "Launch Monday"]
    assert out["risks"] == ["Legal review open"]
    assert out["summary"] == "Clean summary."
