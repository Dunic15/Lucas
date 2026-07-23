"""Hardening regressions for natural multiparty meeting turns."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.meeting.conversation_frame import (  # noqa: E402
    ConversationFrame,
    classify_interruption,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Yes, exactly.", "confirmation"),
        ("Mm-hm.", "confirmation"),
        ("Sorry, Tuesday, not Monday.", "correction"),
        ("Scusa, martedì invece.", "correction"),
    ],
)
def test_natural_interruption_language(text, expected):
    assert classify_interruption(text) == expected


def test_named_imperative_without_question_mark_requests_response():
    frame = ConversationFrame.create(
        avatar_names=["Laura"], capabilities={"Laura": ["calendar"]}
    )
    frame.observe(
        {"kind": "participant_join", "participant_id": "human-1", "speaker_kind": "human"}
    )
    frame.observe(
        {
            "kind": "utterance",
            "participant_id": "human-1",
            "speaker_kind": "human",
            "text": "Laura, show me the calendar",
            "addressed_avatar": "Laura",
        }
    )

    snapshot = frame.snapshot()
    assert snapshot["open_question"] is True
    assert snapshot["selected_avatar"] == "Laura"
    assert snapshot["response_mode"] == "direct"


def test_action_target_switch_between_avatars_seals_boundary():
    frame = ConversationFrame.create(
        avatar_names=["Laura", "Petra"],
        capabilities={"Laura": ["calendar"], "Petra": ["project"]},
    )
    frame.observe(
        {
            "kind": "utterance",
            "participant_id": "human-1",
            "speaker_kind": "human",
            "text": "Laura, schedule the review",
            "addressed_avatar": "Laura",
            "action": {"verb": "schedule"},
        }
    )
    frame.observe(
        {
            "kind": "utterance",
            "participant_id": "human-1",
            "speaker_kind": "human",
            "text": "Petra, add the launch task",
            "addressed_avatar": "Petra",
            "action": {"verb": "add_task"},
        }
    )

    snapshot = frame.snapshot()
    assert snapshot["action_boundary"] == "sealed_on_target_change"
    assert snapshot["selected_avatar"] == "Petra"
    assert snapshot["action_count"] == 2


def test_explicit_avatar_handoff_seals_active_action():
    frame = ConversationFrame.create(
        avatar_names=["Laura", "Petra"],
        capabilities={"Laura": ["calendar"], "Petra": ["project"]},
    )
    frame.observe(
        {
            "kind": "utterance",
            "participant_id": "human-1",
            "speaker_kind": "human",
            "text": "Laura, schedule the review",
            "addressed_avatar": "Laura",
            "action": {"verb": "schedule"},
        }
    )
    frame.observe(
        {"kind": "avatar_handoff", "from_avatar": "Laura", "to_avatar": "Petra"}
    )

    snapshot = frame.snapshot()
    assert snapshot["action_boundary"] == "sealed_on_target_change"
    assert snapshot["selected_avatar"] == "Petra"
    assert snapshot["handoff_status"] == "accepted"
