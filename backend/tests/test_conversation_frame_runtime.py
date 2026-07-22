"""Unit coverage for the shadow ConversationFrame runtime."""
from __future__ import annotations

from types import SimpleNamespace

from app.meeting.conversation_frame import (
    ConversationFrame,
    observe_session_participant,
    observe_session_utterance,
)


def test_snapshot_never_contains_utterance_text():
    secret = "synthetic-sensitive-sentence"
    frame = ConversationFrame.create(
        avatar_names=["Laura"], capabilities={"Laura": ["meeting"]}
    )
    frame.observe(
        {
            "kind": "utterance",
            "participant_id": "human-1",
            "speaker_kind": "human",
            "text": secret,
            "speaker_confidence": 0.4,
        }
    )

    assert secret not in repr(frame)
    assert secret not in repr(frame.snapshot())
    assert frame.snapshot()["attribution_style"] == "neutral"


def test_live_adapter_seeds_roster_and_stays_shadow_only():
    session = SimpleNamespace(
        participants={
            "human-1": {"here": True, "kind": "human"},
            "agent-1": {"here": True, "kind": "agent"},
        },
        conversation_frame=None,
        speaking_until=0.0,
    )

    snapshot = observe_session_utterance(
        session,
        avatar_name="Laura",
        participant_id="human-1",
        speaker_kind="human",
        text="What should we do next?",
    )

    assert snapshot["participant_count"] == 1
    assert snapshot["selected_avatar"] == "Laura"
    assert snapshot["response_mode"] == "direct"
    assert not hasattr(session, "pending_messages")


def test_live_participant_leave_updates_meeting_size():
    session = SimpleNamespace(
        participants={}, conversation_frame=None, speaking_until=0.0
    )
    for participant_id in ("human-1", "human-2"):
        observe_session_participant(
            session,
            avatar_name="Laura",
            participant_id=participant_id,
            speaker_kind="human",
            here=True,
        )
    snapshot = observe_session_participant(
        session,
        avatar_name="Laura",
        participant_id="human-2",
        speaker_kind="human",
        here=False,
    )

    assert snapshot["meeting_size"] == "one_to_one"
    assert snapshot["participant_count"] == 1
