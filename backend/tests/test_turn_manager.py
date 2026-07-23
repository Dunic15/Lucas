"""Behavioral tests for the ConversationFrame-backed Turn Manager."""
from __future__ import annotations

from app.meeting.turn_manager import is_active, normalize_mode, plan_turn


def _snapshot(*, size="small_group", addressed="room", question=True):
    return {
        "meeting_size": size,
        "addressed_to": addressed,
        "open_question": question,
    }


def test_rollout_mode_fails_closed():
    assert normalize_mode("OFF") == "off"
    assert normalize_mode("shadow") == "shadow"
    assert normalize_mode("on") == "on"
    assert normalize_mode("unexpected") == "shadow"
    assert not is_active("shadow")
    assert is_active("on")


def test_named_and_engaged_turns_keep_direct_behavior():
    named = plan_turn(
        _snapshot(size="large_group"),
        called=True,
        followup=False,
        turn_completeness=1.0,
        locked_dyad=True,
    )
    followup = plan_turn(
        _snapshot(size="large_group"),
        called=False,
        followup=True,
        turn_completeness=1.0,
        locked_dyad=True,
    )

    assert named.policy == "direct_address"
    assert not named.suppress_interjection
    assert followup.policy == "engaged_followup"
    assert not followup.suppress_interjection


def test_finished_one_to_one_question_skips_only_deference_wait():
    plan = plan_turn(
        _snapshot(size="one_to_one"),
        called=False,
        followup=False,
        turn_completeness=0.8,
    )

    assert plan.policy == "one_to_one_fast"
    assert plan.skip_deference
    assert not plan.suppress_interjection


def test_incomplete_one_to_one_turn_keeps_deference():
    plan = plan_turn(
        _snapshot(size="one_to_one"),
        called=False,
        followup=False,
        turn_completeness=0.5,
    )

    assert plan.policy == "observe"
    assert not plan.skip_deference


def test_small_group_keeps_human_first_deference():
    plan = plan_turn(
        _snapshot(size="small_group"),
        called=False,
        followup=False,
        turn_completeness=1.0,
    )

    assert plan.policy == "small_group"
    assert not plan.skip_deference
    assert not plan.suppress_interjection


def test_addressed_participant_and_locked_dyad_suppress_interjection():
    participant = plan_turn(
        _snapshot(addressed="participant"),
        called=False,
        followup=False,
        turn_completeness=1.0,
    )
    dyad = plan_turn(
        _snapshot(),
        called=False,
        followup=False,
        turn_completeness=1.0,
        locked_dyad=True,
    )

    assert participant.policy == "yield_to_participant"
    assert participant.suppress_interjection
    assert dyad.policy == "locked_dyad"
    assert dyad.suppress_interjection


def test_large_group_defaults_to_non_interjecting_policy():
    plan = plan_turn(
        _snapshot(size="large_group"),
        called=False,
        followup=False,
        turn_completeness=1.0,
    )

    assert plan.policy == "large_group"
    assert not plan.skip_deference
    assert plan.suppress_interjection
