"""MeetingState: silent per-line tracking + closing intervention logic."""
from __future__ import annotations

import pytest

from app import meeting_state
from app.meeting_state import (
    MeetingState,
    ProcessTemplate,
    build_from_text,
    intervention_line,
    state_summary,
    update,
)

ONBOARDING = ProcessTemplate(
    id="customer_onboarding",
    name="Customer Onboarding",
    required_steps=(
        "security_approval",
        "dpa_confirmation",
        "implementation_owner",
        "customer_handoff",
        "go_live_date",
    ),
    critical_gaps=("security_approval", "dpa_confirmation", "implementation_owner"),
)


def _feed(state: MeetingState, *lines: tuple[str, str]) -> MeetingState:
    for speaker, text in lines:
        update(state, speaker, text, templates=[ONBOARDING], wake_words=["laura"])
    return state


def test_meeting_type_detected_from_hint():
    state = _feed(MeetingState(), ("Ana", "Kickoff for the Acme onboarding."))
    assert state.meeting_type == "customer_onboarding"
    assert state.required_steps == list(ONBOARDING.required_steps)
    assert state.missing_steps == list(ONBOARDING.required_steps)
    assert state.stage == "in_progress"


def test_step_completes_on_done_cue():
    state = _feed(
        MeetingState(),
        ("Ana", "Let's kick off the onboarding."),
        ("Ben", "Security review is approved, we got the sign-off yesterday."),
    )
    assert "security_approval" in state.completed_steps
    assert "security_approval" not in state.missing_steps


def test_pending_mention_does_not_complete_step():
    state = _feed(
        MeetingState(),
        ("Ana", "Let's kick off the onboarding."),
        ("Ben", "We still need security approval before anything ships."),
    )
    assert "security_approval" not in state.completed_steps
    assert "security_approval" in state.missing_steps


def test_dpa_and_go_live_completion():
    state = _feed(
        MeetingState(),
        ("Ana", "New customer onboarding for Acme."),
        ("Ben", "The DPA is signed and confirmed."),
        ("Ana", "Go-live is scheduled for March 3rd."),
    )
    assert "dpa_confirmation" in state.completed_steps
    assert "go_live_date" in state.completed_steps


def test_owner_deadline_decision_risk_question_extraction():
    state = _feed(
        MeetingState(),
        ("Ana", "Kickoff for the Acme onboarding."),
        ("Ben", "Sara will own implementation for this account."),
        ("Ana", "We agreed to go with the standard rollout plan."),
        ("Ben", "The migration script needs to be done by Friday."),
        ("Ana", "I'm worried the timeline is a real risk."),
        ("Ben", "Who is handling the security sign-off?"),
    )
    assert state.owners and state.owners[0]["owner"] == "Sara"
    assert "implementation_owner" in state.completed_steps
    assert state.decisions
    assert state.deadlines and state.deadlines[0]["when"].lower() == "friday"
    assert state.risks
    assert state.open_questions


def test_self_owner_uses_speaker_name():
    state = _feed(MeetingState(), ("Sara", "I'll take the follow-up with legal."))
    assert state.owners and state.owners[0]["owner"] == "Sara"


def test_question_to_avatar_not_tracked_as_open_question():
    state = _feed(MeetingState(), ("Ana", "Laura, who owns the security approval?"))
    assert state.open_questions == []


def test_closing_with_critical_gaps_sets_intervention():
    state = _feed(
        MeetingState(),
        ("Ana", "Kickoff for the Acme onboarding."),
        ("Ben", "Go-live is scheduled for March 3rd."),
        ("Ana", "Great, anything else before we wrap up?"),
    )
    assert state.stage == "wrapping_up"
    assert state.should_intervene
    assert "security approval" in state.intervention_reason
    line = intervention_line(state)
    assert line.startswith("Before we close, I didn't hear security approval")
    assert "DPA confirmation" in line
    assert line.endswith("Should we assign owners before provisioning?")


def test_no_intervention_when_critical_steps_covered():
    state = _feed(
        MeetingState(),
        ("Ana", "Kickoff for the Acme onboarding."),
        ("Ben", "Security approved everything, sign-off is done."),
        ("Ana", "DPA is signed and confirmed."),
        ("Ben", "Sara will own implementation."),
        ("Ana", "Anything else before we wrap up?"),
    )
    assert state.missing_critical() == []
    assert not state.should_intervene
    assert intervention_line(state) == ""


def test_no_intervention_without_template_match():
    state = _feed(
        MeetingState(),
        ("Ana", "Quick sync about the offsite."),
        ("Ben", "That's all, let's wrap up."),
    )
    assert state.meeting_type == ""
    assert not state.should_intervene


def test_readiness_score():
    state = _feed(
        MeetingState(),
        ("Ana", "Kickoff for the Acme onboarding."),
        ("Ben", "Security approved everything."),
        ("Ana", "The DPA is signed."),
    )
    assert state.readiness_score() == 40  # 2 of 5 steps
    assert MeetingState().readiness_score() == 0


def test_build_from_text_parses_speaker_lines():
    text = (
        "Ana: Kickoff for the Acme onboarding.\n"
        "Ben: Security review is approved and cleared.\n"
        "Ana: anything else before we close?"
    )
    avatar = _FakeAvatar()
    state = build_from_text(avatar, text)
    assert state.meeting_type == "customer_onboarding"
    assert "security_approval" in state.completed_steps
    assert state.stage == "wrapping_up"


def test_state_summary_contains_no_headers_only_facts():
    state = _feed(
        MeetingState(),
        ("Ana", "Kickoff for the Acme onboarding."),
        ("Ben", "Security approved everything."),
    )
    summary = state_summary(state)
    assert "customer_onboarding" in summary
    assert "security approval" in summary
    assert "CRITICAL" in summary  # missing critical steps flagged for the LLM


def test_to_dict_has_issue_schema_keys():
    keys = MeetingState().to_dict().keys()
    for expected in (
        "meeting_type",
        "stage",
        "required_steps",
        "completed_steps",
        "missing_steps",
        "decisions",
        "owners",
        "deadlines",
        "risks",
        "open_questions",
        "should_intervene",
        "intervention_reason",
    ):
        assert expected in keys


def test_proactive_flag_deterministic_on_critical_gap():
    """Critical gap at wrap-up -> templated line, no model, no retrieval."""
    from app.brain.engine import proactive_flag

    state = _feed(
        MeetingState(),
        ("Ana", "Kickoff for the Acme onboarding."),
        ("Ben", "Sara will own implementation."),
        ("Ana", "Anything else before we wrap up?"),
    )
    flag = proactive_flag(_FakeAvatar(), "", state=state)
    assert flag["should_speak"] is True
    assert flag["confidence"] >= 0.9
    assert flag["line"].startswith("Before we close, I didn't hear")
    assert flag["citations"] == []  # spoken line must not get a doc suffix
    assert flag["missing_steps"] == ["security_approval", "dpa_confirmation"]


def test_laura_sample_meeting_demos_readiness_gaps():
    """The shipped demo sample must keep telling the product story: a customer
    onboarding at readiness 40 with DPA, security approval, and implementation
    owner still open (and the owner deliberately unclear)."""
    from app import avatars

    avatar = avatars.load("laura")
    state = build_from_text(avatar, (avatar.dir / "sample_meeting.txt").read_text())
    assert state.meeting_type == "customer_onboarding"
    assert state.readiness_score() == 40
    assert set(state.missing_steps) == {
        "security_approval",
        "dpa_confirmation",
        "implementation_owner",
    }
    assert state.owners == []


def test_post_meeting_artifact_has_full_schema(monkeypatch):
    """Stub-mode artifact carries the expanded schema, with missing_steps and
    readiness_score computed deterministically from the process template."""
    import app.brain.engine as brain

    # post_meeting() gates on post_provider() (per-path provider split), so pin
    # that too; patching only effective_provider let a real key drive the model.
    monkeypatch.setattr(brain, "effective_provider", lambda: "stub")
    monkeypatch.setattr(brain, "post_provider", lambda: "stub")
    text = (
        "Ana: Kickoff for the Acme onboarding.\n"
        "Ben: Security review is approved and cleared.\n"
        "Ana: We agreed to go with the standard rollout plan.\n"
        "Ben: I'm worried the data migration is a risk.\n"
    )
    artifact = brain.post_meeting(_FakeAvatar(), text)
    for key in (
        "summary",
        "decisions",
        "actions",
        "checklist",
        "missing_steps",
        "readiness_score",
        "risks",
        "follow_up_email",
    ):
        assert key in artifact, key
    assert artifact["readiness_score"] == 20  # 1 of 5 steps covered
    assert "security_approval" not in artifact["missing_steps"]
    assert "dpa_confirmation" in artifact["missing_steps"]
    assert artifact["decisions"]
    assert artifact["risks"]
    assert artifact["checklist"] == artifact["actions"]  # back-compat alias


class _FakeAvatar:
    """Just enough Avatar surface for build_from_text / stub post_meeting."""

    id = "_fake_meeting_state_test"
    name = "Laura"
    wake_words = ["laura"]

    def __init__(self):
        from pathlib import Path

        self.dir = Path("/nonexistent")


@pytest.fixture(autouse=True)
def _fake_templates(monkeypatch):
    """build_from_text on the fake avatar should see the onboarding template."""
    real = meeting_state.templates_for

    def patched(avatar):
        if getattr(avatar, "id", "") == _FakeAvatar.id:
            return [ONBOARDING]
        return real(avatar)

    monkeypatch.setattr(meeting_state, "templates_for", patched)


# ── per-person tracking ──


def test_per_person_tracks_commitments_questions_risks():
    state = _feed(
        MeetingState(),
        ("Marco", "I'll take the rollout plan."),
        ("Anna", "Who owns the security review?"),
        ("Anna", "I'm worried the timeline could slip."),
        ("Duccio", "Marco will own the vendor follow-up too."),
    )
    marco = state.per_person["Marco"]
    assert any("rollout" in c for c in marco["commitments"])  # self-commitment
    assert any("vendor" in c for c in marco["commitments"])   # assigned by Duccio
    anna = state.per_person["Anna"]
    assert any("security review" in q for q in anna["questions"])
    assert any("slip" in r for r in anna["risks"])
    assert anna["lines"] == 2


def test_per_person_merges_owner_first_name_with_full_speaker_name():
    state = _feed(
        MeetingState(),
        ("Marco Rossi", "Happy to help where needed."),
        ("Duccio", "Let's assign it to Marco please."),
    )
    assert "Marco Rossi" in state.per_person
    assert "Marco" not in state.per_person  # merged, not duplicated
    assert state.per_person["Marco Rossi"]["commitments"]


def test_per_person_excludes_explicit_agent_but_keeps_same_name_human():
    state = MeetingState()
    update(
        state,
        "Laura",
        "I'll take the notes for this meeting.",
        participant_id="bot-participant",
        speaker_kind="agent",
        templates=[ONBOARDING],
        wake_words=["laura"],
    )
    update(
        state,
        "Laura",
        "I'll take the human follow-up.",
        participant_id="human-laura",
        speaker_kind="human",
        templates=[ONBOARDING],
        wake_words=["laura"],
    )
    assert "bot-participant" not in state.per_person
    assert state.per_person["human-laura"]["name"] == "Laura"


def test_per_person_in_summary_and_dict():
    state = _feed(
        MeetingState(),
        ("Marco", "I'll handle the DPA follow-up."),
    )
    summary = state_summary(state)
    assert "Per person:" in summary
    assert "Marco" in summary
    assert "per_person" in state.to_dict()


def test_per_person_lists_stay_bounded():
    state = MeetingState()
    for i in range(30):
        update(
            state,
            "Marco",
            f"I'll take task number {i} for the team.",
            wake_words=["laura"],
        )
    assert len(state.per_person["Marco"]["commitments"]) <= 8


# ── readiness fallback: a non-onboarding meeting still scores a real value ──


def test_readiness_derived_when_no_template(monkeypatch):
    """A generic (non-onboarding) transcript matches no process template, so the
    rigorous step-coverage readiness is undefined. The artifact must STILL carry
    a real, defensible readiness_score (never a dead 0/"-" on the demo tile)."""
    import app.brain.engine as brain

    monkeypatch.setattr(brain, "effective_provider", lambda: "stub")
    monkeypatch.setattr(brain, "post_provider", lambda: "stub")
    text = (
        "Marco: Let's review the quarterly numbers. I'll send the deck to the team.\n"
        "Anna: I can draft the summary email by Friday.\n"
        "Marco: We decided to move the launch to Q3.\n"
        "Anna: One risk is the vendor timeline could slip.\n"
    )
    artifact = brain.post_meeting(_FakeAvatar(), text)
    assert artifact["meeting_type"] == ""        # no template matched
    score = artifact["readiness_score"]
    assert isinstance(score, int)
    assert 0 < score <= 100                       # real value, never a dead "-"


def test_derived_readiness_weights():
    """_derived_readiness is a deterministic 0-100 from distilled fields only."""
    import app.brain.engine as brain

    assert brain._derived_readiness({}) == 0
    assert brain._derived_readiness({"summary": "recap"}) == 25
    unassigned = {
        "summary": "recap",
        "actions": [{"item": "x", "owner": "UNASSIGNED"}],
        "follow_up_email": {"subject": "Follow-up", "body": "…"},
    }
    assert brain._derived_readiness(unassigned) == 70   # 25 + 25 + 0 + 20
    owned = dict(unassigned, actions=[{"item": "x", "owner": "Ben"}])
    assert brain._derived_readiness(owned) == 100        # 25 + 25 + 30 + 20

