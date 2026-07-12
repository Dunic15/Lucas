"""Post-meeting actions come from the meeting, never its injected brief."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, brain, main
from app.config import settings


def test_brief_only_actions_do_not_enter_artifact(monkeypatch):
    """Prod repro: one spoken ask plus two open items in Cedric's brief.

    Even if the summarizer over-extracts all three, the two actions supported
    only by the injected pre-meeting context must be removed.
    """
    transcript = "Cedric, manda la mail di riepilogo a Marco Rossi"
    brief = (
        "MEETING BRIEF (from the orchestrator):\n"
        "- Complete security review before pilot start\n"
        "- Deliver rollout doc to client"
    )
    model_artifact = {
        "summary": "Cedric was asked to send a recap email.",
        "decisions": [],
        "actions": [
            {
                "item": "Send the recap email to Marco Rossi",
                "owner": "Cedric",
                "deadline": "",
                "gap_type": "none",
                "evidence": transcript,
            },
            {
                "item": "Complete security review before pilot start",
                "owner": "UNASSIGNED",
                "deadline": "",
                "gap_type": "owner",
                "evidence": "Complete security review before pilot start",
            },
            {
                "item": "Deliver rollout doc to client",
                "owner": "UNASSIGNED",
                "deadline": "",
                "gap_type": "owner",
                "evidence": "Deliver rollout doc to client",
            },
        ],
        "risks": [],
        "follow_up_email": {},
    }
    seen = {}

    def fake_complete(system, user, **kwargs):
        seen["system"] = system
        seen["user"] = user
        return json.dumps(model_artifact)

    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(brain.llm, "complete", fake_complete)
    monkeypatch.setattr(brain, "retrieve", lambda avatar, query, k=6: [])

    artifact = brain.post_meeting(avatars.load("cedric"), transcript, context=brief)

    assert [action["item"] for action in artifact["actions"]] == [
        "Send the recap email to Marco Rossi"
    ]
    assert artifact["checklist"] is artifact["actions"]
    assert "evidence" not in artifact["actions"][0]
    assert "only source" in seen["system"]
    assert "BACKGROUND ONLY" in seen["user"]


def test_live_captures_are_not_subject_to_model_evidence_filter():
    """The evidence guard applies only to summarizer output, not queue_action."""
    artifact = {
        "actions": [
            {
                "item": "Brief-only task",
                "owner": "UNASSIGNED",
                "evidence": "Not present in the transcript",
            }
        ]
    }

    brain._scope_actions_to_transcript(artifact, "A short meeting with no task")
    merged = main._merge_action_items(
        [{"action": "Send the recap to Marco Rossi", "owner": "Cedric"}],
        artifact["actions"],
    )

    assert artifact["actions"] == []
    assert artifact["checklist"] == []
    assert [action["item"] for action in merged] == ["Send the recap to Marco Rossi"]
    assert merged[0]["requested_live"] is True
