from __future__ import annotations

from tests.eval_laura_onboarding import (
    REQUIRED_ARTIFACT_KEYS,
    build_meeting_state,
    build_post_meeting_artifact,
    load_scenarios,
    run_all,
)

from app import avatars, meeting_state


def _scenario(name: str) -> str:
    for scenario_name, transcript, _expected in load_scenarios():
        if scenario_name == name:
            return transcript
    raise AssertionError(f"unknown scenario: {name}")


def test_eval_runner_scenarios_pass():
    results = run_all()
    assert results
    assert all(result.passed for result in results)


def test_readiness_score_calculation_on_synthetic_meetings():
    complete_state = build_meeting_state(_scenario("all_complete"))
    dpa_missing_state = build_meeting_state(_scenario("dpa_missing"))

    assert complete_state.readiness_score() == 100
    assert dpa_missing_state.readiness_score() == 80


def test_laura_onboarding_template_loads_required_steps():
    avatar = avatars.load("laura")
    templates = meeting_state.templates_for(avatar)
    onboarding = next(t for t in templates if t.id == "customer_onboarding")

    assert onboarding.required_steps == (
        "security_approval",
        "dpa_confirmation",
        "implementation_owner",
        "customer_handoff",
        "go_live_date",
    )
    assert onboarding.critical_gaps == (
        "security_approval",
        "dpa_confirmation",
        "implementation_owner",
    )


def test_missing_step_detection_on_synthetic_scenarios():
    expected_missing = {
        "ambiguous_owner_not_assigned": ["implementation_owner"],
        "dpa_missing": ["dpa_confirmation"],
        "dpa_legal_handle_not_confirmed": ["dpa_confirmation"],
        "security_approval_missing": ["security_approval"],
        "security_approval_completed": [],
        "implementation_owner_missing": ["implementation_owner"],
        "go_live_date_missing": ["go_live_date"],
        "all_complete": [],
    }

    for name, missing_steps in expected_missing.items():
        state = build_meeting_state(_scenario(name))
        assert state.missing_steps == missing_steps


def test_post_meeting_artifact_schema_contains_required_fields():
    artifact = build_post_meeting_artifact(_scenario("all_complete"))

    assert REQUIRED_ARTIFACT_KEYS.issubset(artifact.keys())
    assert artifact["transcript"]
    assert isinstance(artifact["decisions"], list)
    assert isinstance(artifact["actions"], list)
    assert isinstance(artifact["missing_steps"], list)
    assert isinstance(artifact["readiness_score"], int)
    assert isinstance(artifact["risks"], list)
    assert isinstance(artifact["follow_up_email"], dict)
