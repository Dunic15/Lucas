from __future__ import annotations

import json

from tests.eval_live_realism import (
    BASELINE_THRESHOLD,
    DIMENSIONS,
    load_scenarios,
    run_all,
)


def test_live_realism_composite_stays_above_baseline():
    report = run_all()

    assert report.realism_score >= BASELINE_THRESHOLD, json.dumps(
        report.to_dict(), indent=2, sort_keys=True
    )


def test_live_realism_corpus_covers_required_scenario_types():
    tags = {
        tag
        for _name, _transcript, expected in load_scenarios()
        for tag in expected["covers"]
    }

    assert {
        "single_human_qa",
        "multiparty_cross_talk",
        "trailing_off",
        "filler_heavy_rambling",
        "code_switched_it_en",
        "rapid_back_and_forth",
        "direct_vs_third_person",
        "fuzzy_name_near_miss",
        "heated_disagreement",
        "wrap_up_closing",
        "must_stay_silent",
    } <= tags


def test_every_turn_has_all_six_dimension_labels():
    for name, transcript, expected in load_scenarios():
        assert len(transcript) == len(expected["turns"]), name
        for turn in expected["turns"]:
            assert set(DIMENSIONS) <= set(turn), (name, turn["id"])


def test_known_runtime_findings_are_explicit_xfails_only():
    report = run_all()

    assert all(finding["reason"].strip() for finding in report.xfails)
    assert not report.unexpected_mismatches, json.dumps(
        report.unexpected_mismatches, indent=2, sort_keys=True
    )
