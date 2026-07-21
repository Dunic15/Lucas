"""New process templates: implementation_access, decision_quality, meeting_readiness.

Each template must (a) lock its meeting type from a first-line hint, (b) mark its
required steps covered from realistic phrasing, and (c) fire Laura's one closing
intervention exactly when a CRITICAL step never happened; and stay silent when the
critical steps were covered. These are the same guarantees the shipped
customer_onboarding template already has (see test_meeting_state.py).
"""
from __future__ import annotations

from app.meeting_state import (
    MeetingState,
    ProcessTemplate,
    intervention_line,
    update,
)

IMPL_ACCESS = ProcessTemplate(
    id="implementation_access",
    name="Implementation Access",
    required_steps=(
        "access_granted",
        "technical_owner",
        "environment_ready",
        "integration_scope",
        "kickoff_scheduled",
    ),
    critical_gaps=("access_granted", "technical_owner", "environment_ready"),
)
DECISION_QUALITY = ProcessTemplate(
    id="decision_quality",
    name="Decision Quality",
    required_steps=(
        "options_considered",
        "decision_made",
        "decision_owner",
        "success_criteria",
        "next_step_defined",
    ),
    critical_gaps=("decision_made", "decision_owner", "next_step_defined"),
)
MEETING_READINESS = ProcessTemplate(
    id="meeting_readiness",
    name="Meeting Readiness",
    required_steps=(
        "objective_clear",
        "agenda_set",
        "right_attendees",
        "pre_read_shared",
        "decisions_needed_listed",
    ),
    critical_gaps=("objective_clear", "decisions_needed_listed"),
)


def _feed(template: ProcessTemplate, *lines: tuple[str, str]) -> MeetingState:
    state = MeetingState()
    for speaker, text in lines:
        update(state, speaker, text, templates=[template], wake_words=["laura"])
    return state


# ───────────────────────── implementation_access ──────────────────────
def test_impl_access_type_and_covered_no_intervention():
    state = _feed(
        IMPL_ACCESS,
        ("Sam", "Let's grant the team access and set up the environment."),
        ("Dana", "Access is granted and credentials are provisioned."),
        ("Sam", "The staging environment is ready."),
        ("Dana", "Priya will be the technical owner."),
        ("Sam", "Okay that's everything, let's wrap up."),
    )
    assert state.meeting_type == "implementation_access"
    assert {"access_granted", "environment_ready", "technical_owner"} <= set(
        state.completed_steps
    )
    assert state.stage == "wrapping_up"
    assert state.missing_critical() == []
    assert not state.should_intervene


def test_impl_access_missing_technical_owner_intervenes():
    state = _feed(
        IMPL_ACCESS,
        ("Sam", "We need to grant the team access to the environment."),
        ("Dana", "Access is granted and the sandbox environment is ready."),
        ("Sam", "Anything else before we wrap up?"),
    )
    assert state.should_intervene
    assert "technical_owner" in state.missing_critical()
    line = intervention_line(state)
    assert line.startswith("Before we close, I didn't hear technical owner")
    assert line.endswith("Should we sort access and an owner before the team starts?")


# ─────────────────────────── decision_quality ─────────────────────────
def test_decision_quality_full_decision_no_intervention():
    state = _feed(
        DECISION_QUALITY,
        ("Lee", "This is a decision meeting to decide between Vendor A and Vendor B."),
        ("Mo", "We weighed the options and the trade-offs."),
        ("Lee", "We've decided to go with Vendor A."),
        ("Mo", "Priya owns the decision."),
        ("Lee", "The next step is the contract, due Friday."),
        ("Mo", "Great, that's everything, let's wrap up."),
    )
    assert state.meeting_type == "decision_quality"
    assert {"options_considered", "decision_made", "decision_owner", "next_step_defined"} <= set(
        state.completed_steps
    )
    assert not state.should_intervene


def test_decision_quality_no_decision_intervenes():
    state = _feed(
        DECISION_QUALITY,
        ("Lee", "This is a decision meeting to decide which vendor."),
        ("Mo", "We considered the options and trade-offs at length."),
        ("Lee", "Still not sure. Anyway, that's everything, let's wrap up."),
    )
    assert state.should_intervene
    assert "decision_made" in state.missing_critical()
    assert intervention_line(state).endswith(
        "Should we lock the decision and its owner before we move on?"
    )


# ────────────────────────── meeting_readiness ─────────────────────────
def test_meeting_readiness_ready_scores_full_no_intervention():
    state = _feed(
        MEETING_READINESS,
        ("Ana", "Let's start our meeting readiness review; what's the objective?"),
        ("Bo", "The objective is to approve the Q3 budget."),
        ("Ana", "I've set the agenda and shared the pre-read deck."),
        ("Bo", "The right attendees are the finance leads; I'll invite them."),
        ("Ana", "The decisions on the table are the budget approval and the vendor choice."),
        ("Bo", "Perfect, that's everything."),
    )
    assert state.meeting_type == "meeting_readiness"
    assert state.readiness_score() == 100
    assert not state.should_intervene


def test_meeting_readiness_question_does_not_cover_objective():
    """Merely ASKING 'what's the objective?' must not mark objective_clear covered."""
    state = _feed(
        MEETING_READINESS,
        ("Ana", "Let's start our meeting readiness review; what's the objective?"),
    )
    assert "objective_clear" not in state.completed_steps


def test_meeting_readiness_not_ready_intervenes():
    state = _feed(
        MEETING_READINESS,
        ("Ana", "Let's start our meeting readiness prep."),
        ("Bo", "I'll set an agenda and invite the finance leads."),
        ("Ana", "Okay that's all for now, let's wrap up."),
    )
    assert state.should_intervene
    assert {"objective_clear", "decisions_needed_listed"} <= set(state.missing_critical())


# ───────────── the real Laura avatar ships all four templates ──────────
def test_laura_avatar_loads_all_templates():
    from app import avatars
    from app.meeting_state import templates_for

    ids = {t.id for t in templates_for(avatars.load("laura"))}
    assert {
        "customer_onboarding",
        "implementation_access",
        "decision_quality",
        "meeting_readiness",
    } <= ids
