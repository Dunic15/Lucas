"""Post-meeting artifact never ships a garbled recap, even when the model emits
malformed JSON or echoes transcript chunks into a list field. Deterministic —
no model, no keys."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import brain, meeting_state


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
