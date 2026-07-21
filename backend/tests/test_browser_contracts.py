"""Browser B0 demo-handoff contracts; key-free.

The stable typed shapes the demo integrator + Control Center design branch code
against: BrowserObservation, VisualPlanner proposals, CommandResult, visual
verification verdicts, demo-run metadata.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser import contracts, planner


# ── BrowserObservation ──────────────────────────────────────────────────────

def test_build_observation_has_all_stable_fields():
    sanitized = {"url": "https://x/p", "title": "P", "dom_summary": "hello",
                 "elements": [{"id": "a", "role": "link", "name": "A",
                               "kind": "link", "sensitive": False}],
                 "screenshot_ref": "shot:x", "truncated": False}
    obs = contracts.build_observation(
        session_id="s1", command_sequence=3, page_version=2,
        sanitized=sanitized, timestamp=1789459200.0)
    for field in contracts.OBSERVATION_FIELDS:
        assert field in obs, field
    assert obs["session_id"] == "s1"
    assert obs["command_sequence"] == 3
    assert obs["page_version"] == 2
    assert obs["visible_text"] == "hello"
    assert obs["viewport"] == {"width": 1280, "height": 720}


# ── CommandResult ───────────────────────────────────────────────────────────

def test_command_result_shape_and_failure_categories():
    r = contracts.command_result(
        accepted=True, command_sequence=1, page_version=1,
        classification="auto")
    for field in ("accepted", "command_sequence", "page_version",
                  "classification", "observation_ref", "action_id",
                  "failure_category", "replanning_permitted", "verification"):
        assert field in r, field
    assert r["accepted"] is True
    assert r["failure_category"] == ""
    # a rejected result names a known failure category
    rj = contracts.command_result(
        accepted=False, command_sequence=2, page_version=1,
        classification="blocked", failure_category="blocked",
        replanning_permitted=False)
    assert rj["failure_category"] in contracts.FAILURE_CATEGORIES


# ── visual verification ─────────────────────────────────────────────────────

def test_verify_expectation_verified_not_verified_inconclusive():
    obs = {"url": "https://demo/pricing", "title": "Pricing",
           "visible_text": "Team plan 20 EUR", "page_version": 4,
           "elements": [{"id": "buy"}]}
    assert contracts.verify_expectation(
        {"url_contains": "pricing"}, obs) == contracts.VERIFIED
    assert contracts.verify_expectation(
        {"text_contains": "20 EUR", "element_present": "buy"}, obs) \
        == contracts.VERIFIED
    assert contracts.verify_expectation(
        {"url_contains": "checkout"}, obs) == contracts.NOT_VERIFIED
    assert contracts.verify_expectation(
        {"page_version_at_least": 9}, obs) == contracts.NOT_VERIFIED
    # no recognised keys → inconclusive (never a false "verified")
    assert contracts.verify_expectation({}, obs) == contracts.INCONCLUSIVE
    assert contracts.verify_expectation(
        {"unknown_key": "x"}, obs) == contracts.INCONCLUSIVE


# ── demo-run metadata ───────────────────────────────────────────────────────

def test_clean_metadata_whitelists_and_bounds():
    m = contracts.clean_metadata({
        "demo_definition_id": "onboarding",
        "demo_definition_version": "3",
        "demo_run_id": "run-42",
        "current_checkpoint": "step-2",
        "evil": "should be dropped",
        "state": "ready",  # must not smuggle authoritative fields
    })
    assert set(m.keys()) == set(contracts.METADATA_FIELDS)
    assert "evil" not in m and "state" not in m
    assert contracts.clean_metadata(None) == {}
    long = contracts.clean_metadata({"demo_run_id": "x" * 500})
    assert len(long["demo_run_id"]) == 120


# ── VisualPlanner ───────────────────────────────────────────────────────────

def test_scripted_planner_proposes_and_grounds_in_observation():
    script = [
        {"operation": "navigate", "target": "",
         "arguments": {"url": "https://demo.laura.test/pricing"},
         "expected_result": {"url_contains": "pricing"}},
        {"operation": "click", "target": "buy-team", "consequential": True,
         "reason": "purchase the team plan"},
    ]
    p = planner.ScriptedPlanner(script)
    obs1 = {"page_version": 1, "elements": []}
    prop1 = p.propose(obs1, "buy the plan")
    for field in planner.PROPOSAL_FIELDS:
        assert field in prop1, field
    assert prop1["operation"] == "navigate"
    # Next step targets buy-team but the current observation lacks it →
    # planner returns a REPLAN (observe), not a blind click.
    obs_stale = {"page_version": 2, "elements": [{"id": "other"}]}
    prop_replan = p.propose(obs_stale, "buy the plan")
    assert prop_replan["operation"] == "observe"
    assert "replan" in prop_replan["reason"].lower()
    # When buy-team IS present, it proposes the consequential click.
    obs_ready = {"page_version": 2, "elements": [{"id": "buy-team"}]}
    prop2 = p.propose(obs_ready, "buy the plan")
    assert prop2["operation"] == "click" and prop2["consequential"] is True


def test_planner_confidence_clamped():
    prop = planner.proposal(operation="observe", confidence=5.0)
    assert prop["confidence"] == 1.0
    prop2 = planner.proposal(operation="observe", confidence=-2.0)
    assert prop2["confidence"] == 0.0
