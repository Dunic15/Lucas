"""Executable contract for multiparty ConversationFrame shadow mode.

This PR intentionally owns tests/evals only.  The production adapter is optional
until the backend owner implements ``app.meeting.conversation_frame.replay_shadow``.
The fixture validation tests always run; scenario behavior becomes live
automatically as soon as that adapter exists.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "conversation_frame" / "scenarios.json"
REQUIRED_COMPONENTS = {
    "conversation_frame",
    "turn_separation",
    "multi_avatar_arbitration",
    "interruption_recovery",
}
EVENT_KINDS = {
    "participant_join",
    "participant_leave",
    "utterance",
    "avatar_speech_started",
    "avatar_handoff",
}
PII_SAFE_RESULT_KEYS = {
    "meeting_size",
    "participant_count",
    "addressed_to",
    "open_question",
    "selected_avatar",
    "response_mode",
    "action_boundary",
    "action_count",
    "action_requested_by",
    "action_owner",
    "action_approver",
    "action_target_account",
    "duplicate_responses",
    "handoff_status",
    "interruption_type",
    "response_state",
    "speech_generation",
    "speaker_confidence_band",
    "attribution_style",
}


def _scenarios() -> list[dict[str, Any]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data
    return data


SCENARIOS = _scenarios()


def test_scenario_ids_are_unique_and_synthetic():
    ids = [scenario["id"] for scenario in SCENARIOS]
    assert len(ids) == len(set(ids))
    fixture_text = FIXTURE.read_text(encoding="utf-8").lower()
    for forbidden in ("@", "http://", "https://", "acme", "duccio profeti"):
        assert forbidden not in fixture_text


def test_all_social_behavior_components_are_covered():
    assert {scenario["component"] for scenario in SCENARIOS} == REQUIRED_COMPONENTS
    by_component = {
        component: sum(scenario["component"] == component for scenario in SCENARIOS)
        for component in REQUIRED_COMPONENTS
    }
    assert by_component["conversation_frame"] >= 5
    assert by_component["turn_separation"] >= 1
    assert by_component["multi_avatar_arbitration"] >= 4
    assert by_component["interruption_recovery"] >= 4


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario["id"])
def test_scenario_schema_is_complete(scenario: dict[str, Any]):
    assert scenario["avatar_names"]
    assert isinstance(scenario["capabilities"], dict)
    assert scenario["events"]
    assert scenario["expected"]
    assert set(scenario["expected"]) <= PII_SAFE_RESULT_KEYS
    for event in scenario["events"]:
        assert event["kind"] in EVENT_KINDS
        if event["kind"] == "utterance":
            assert event.get("participant_id")
            assert event.get("speaker_kind") in {"human", "agent"}
            confidence = event.get("speaker_confidence")
            assert confidence is None or 0.0 <= confidence <= 1.0


def test_contract_covers_safety_invariants():
    by_id = {scenario["id"]: scenario for scenario in SCENARIOS}
    assert by_id["generic_question_routes_by_capability"]["expected"]["duplicate_responses"] == 0
    assert by_id["named_avatar_wins_over_capability"]["expected"]["duplicate_responses"] == 0
    assert by_id["target_switch_seals_action"]["expected"]["action_count"] == 1
    assert by_id["small_group_question_for_human"]["expected"]["selected_avatar"] == "none"
    assert by_id["low_speaker_confidence_uses_neutral_attribution"]["expected"]["attribution_style"] == "neutral"


def _runtime_replay():
    try:
        from app.meeting.conversation_frame import replay_shadow
    except ImportError:
        return None
    signature = inspect.signature(replay_shadow)
    assert {"events", "avatar_names", "capabilities"} <= set(signature.parameters)
    return replay_shadow


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario["id"])
def test_runtime_matches_shadow_contract(scenario: dict[str, Any]):
    replay_shadow = _runtime_replay()
    if replay_shadow is None:
        pytest.skip("backend handoff: ConversationFrame runtime adapter not implemented yet")
    actual = replay_shadow(
        events=scenario["events"],
        avatar_names=scenario["avatar_names"],
        capabilities=scenario["capabilities"],
    )
    assert isinstance(actual, dict)
    # A backend implementation may expose extra in-memory fields.  The contract
    # compares only the stable, PII-safe shadow decision surface.
    assert {key: actual.get(key) for key in scenario["expected"]} == scenario["expected"]
