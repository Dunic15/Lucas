"""Lightweight synthetic evals for Laura's onboarding MeetingState path.

Run from the repo root:
    .venv/bin/python tests/eval_laura_onboarding.py
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

os.environ["BRAIN_PROVIDER"] = "stub"
# The post-meeting path gates on BRAIN_PROVIDER_POST (per-path provider split), so
# pin it too — otherwise a real post-provider in .env makes this eval non-
# deterministic. build_post_meeting_artifact also force-overrides the function, so
# this holds even when settings were already constructed by an earlier import.
os.environ["BRAIN_PROVIDER_POST"] = "stub"

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import avatars, meeting_state  # noqa: E402
from app.brain import engine as brain  # noqa: E402  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MEETINGS_DIR = FIXTURES_DIR / "meetings"
EXPECTED_DIR = FIXTURES_DIR / "expected"
REQUIRED_ARTIFACT_KEYS = {
    "transcript",
    "decisions",
    "actions",
    "missing_steps",
    "readiness_score",
    "risks",
    "follow_up_email",
}


@dataclass(frozen=True)
class EvalResult:
    name: str
    passed: bool
    failures: list[str]
    readiness_score: int
    missing_steps: list[str]
    should_proceed: bool


def load_scenarios() -> list[tuple[str, str, dict[str, Any]]]:
    scenarios: list[tuple[str, str, dict[str, Any]]] = []
    for transcript_path in sorted(MEETINGS_DIR.glob("*.txt")):
        expected_path = EXPECTED_DIR / f"{transcript_path.stem}.json"
        if not expected_path.exists():
            raise FileNotFoundError(f"Missing expected output: {expected_path}")
        scenarios.append(
            (
                transcript_path.stem,
                transcript_path.read_text(encoding="utf-8"),
                json.loads(expected_path.read_text(encoding="utf-8")),
            )
        )
    return scenarios


def build_post_meeting_artifact(transcript: str) -> dict[str, Any]:
    """Run the real post-meeting path offline and mirror final stored schema."""
    avatar = avatars.load("laura")
    # post_meeting() gates on post_provider(); force both provider hooks to the
    # deterministic stub so the eval is provider-independent regardless of what
    # keys / BRAIN_PROVIDER_POST are set in the environment.
    original_effective = brain.effective_provider
    original_post = brain.post_provider
    brain.effective_provider = lambda: "stub"
    brain.post_provider = lambda: "stub"
    try:
        artifact = brain.post_meeting(avatar, transcript)
    finally:
        brain.effective_provider = original_effective
        brain.post_provider = original_post
    artifact["transcript"] = transcript
    return artifact


def build_meeting_state(transcript: str) -> meeting_state.MeetingState:
    avatar = avatars.load("laura")
    return meeting_state.build_from_text(avatar, transcript)


def should_proceed(artifact: dict[str, Any]) -> bool:
    return not artifact.get("missing_steps") and artifact.get("readiness_score") == 100


def evaluate_scenario(name: str, transcript: str, expected: dict[str, Any]) -> EvalResult:
    artifact = build_post_meeting_artifact(transcript)
    failures: list[str] = []

    missing = artifact.get("missing_steps", [])
    expected_missing = expected["expected_missing_steps"]
    if missing != expected_missing:
        failures.append(f"missing_steps expected {expected_missing}, got {missing}")

    score = artifact.get("readiness_score")
    score_range = expected["expected_readiness_score"]
    if not (score_range["min"] <= score <= score_range["max"]):
        failures.append(
            "readiness_score expected "
            f"{score_range['min']}..{score_range['max']}, got {score}"
        )

    action_failures = _check_expected_actions(
        artifact.get("actions", []), expected.get("expected_actions", [])
    )
    failures.extend(action_failures)

    risk_failures = _check_expected_risks(
        artifact.get("risks", []), expected.get("expected_risks", [])
    )
    failures.extend(risk_failures)

    proceed = should_proceed(artifact)
    if proceed != expected["should_proceed"]:
        failures.append(f"should_proceed expected {expected['should_proceed']}, got {proceed}")

    missing_schema = sorted(REQUIRED_ARTIFACT_KEYS - set(artifact.keys()))
    if missing_schema:
        failures.append(f"artifact missing schema keys: {missing_schema}")

    return EvalResult(
        name=name,
        passed=not failures,
        failures=failures,
        readiness_score=int(score),
        missing_steps=list(missing),
        should_proceed=proceed,
    )


def run_all() -> list[EvalResult]:
    return [evaluate_scenario(name, transcript, expected) for name, transcript, expected in load_scenarios()]


def _check_expected_actions(
    actual: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> list[str]:
    failures: list[str] = []
    for exp in expected:
        if not any(_action_matches(action, exp) for action in actual):
            failures.append(f"expected action not found: {exp}")
    return failures


def _action_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    item = str(actual.get("item", ""))
    if expected.get("contains") and expected["contains"] not in item:
        return False
    if expected.get("owner") and actual.get("owner") != expected["owner"]:
        return False
    if expected.get("gap_type") and actual.get("gap_type") != expected["gap_type"]:
        return False
    return True


def _check_expected_risks(actual: list[str], expected: list[str]) -> list[str]:
    failures: list[str] = []
    actual_text = "\n".join(str(risk) for risk in actual)
    for exp in expected:
        if exp not in actual_text:
            failures.append(f"expected risk not found: {exp}")
    return failures


def print_summary(results: list[EvalResult]) -> None:
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    failed = total - passed

    print(f"Laura onboarding eval: total={total}, passed={passed}, failed={failed}")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(
            f"{status} {result.name}: readiness={result.readiness_score}, "
            f"missing={result.missing_steps}, should_proceed={result.should_proceed}"
        )
        for failure in result.failures:
            print(f"  - {failure}")

    if failed:
        failing_names = ", ".join(result.name for result in results if not result.passed)
        print(f"\nFailing scenarios: {failing_names}")
    print(f"\n{passed}/{total} onboarding eval scenarios passed")


def main() -> int:
    results = run_all()
    print_summary(results)
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
