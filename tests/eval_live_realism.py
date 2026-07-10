"""Offline conversational-realism eval for Laura's live decision path.

Run from the repository root:
    .venv/bin/python tests/eval_live_realism.py

The corpus is synthetic. Runtime providers are pinned before importing app
modules so this driver remains deterministic and key-free.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

os.environ["BRAIN_PROVIDER"] = "stub"
os.environ["BRAIN_PROVIDER_POST"] = "stub"
os.environ["EMBEDDINGS_PROVIDER"] = "hash"

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import avatars, decision, emotion, end_of_turn, meeting_state  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
LIVE_DIR = FIXTURES_DIR / "live"
EXPECTED_DIR = FIXTURES_DIR / "expected" / "live"

BASELINE_THRESHOLD = 99.0
DEFERENCE_BASE = 1.8
DEFERENCE_LO = 1.0
DEFERENCE_HI = 2.6
ACTIVE_PARTIAL_SECONDS = 0.6

DIMENSIONS = (
    "floor_holding",
    "address_detection",
    "multiparty_deference",
    "control_intent",
    "emotional_appropriateness",
    "intervention_timing",
)

# False speech is the worst live failure, followed by selecting the wrong human.
# Precision-biased F0.5 makes false positives cost more than missed positives in
# those dimensions; the other dimensions use balanced F1.
DIMENSION_WEIGHTS = {
    "floor_holding": 0.14,
    "address_detection": 0.31,
    "multiparty_deference": 0.22,
    "control_intent": 0.13,
    "emotional_appropriateness": 0.10,
    "intervention_timing": 0.10,
}
DIMENSION_BETA = {
    "floor_holding": 1.0,
    "address_detection": 0.5,
    "multiparty_deference": 0.5,
    "control_intent": 1.0,
    "emotional_appropriateness": 1.0,
    "intervention_timing": 1.0,
}
NEGATIVE_LABEL = {
    "floor_holding": None,
    "address_detection": "silent",
    "multiparty_deference": "none",
    "control_intent": "none",
    "emotional_appropriateness": None,
    "intervention_timing": "silent",
}


@dataclass(frozen=True)
class Observation:
    scenario: str
    turn_id: str
    dimension: str
    expected: str
    actual: str
    xfail_reason: str = ""

    @property
    def correct(self) -> bool:
        return self.expected == self.actual


@dataclass(frozen=True)
class DimensionScore:
    precision: float
    recall: float
    score: float
    correct: int
    total: int
    weight: float


@dataclass(frozen=True)
class EvalReport:
    scenarios: int
    turns: int
    dimensions: dict[str, DimensionScore]
    realism_score: float
    false_speech: int
    wrong_target: int
    xfails: list[dict[str, str]]
    unexpected_mismatches: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": "laura_live_realism",
            "scenarios": self.scenarios,
            "turns": self.turns,
            "baseline_threshold": BASELINE_THRESHOLD,
            "realism_score": self.realism_score,
            "dimensions": {
                name: asdict(score) for name, score in self.dimensions.items()
            },
            "safety": {
                "false_speech": self.false_speech,
                "wrong_target": self.wrong_target,
            },
            "xfails": self.xfails,
            "unexpected_mismatches": self.unexpected_mismatches,
        }


def load_scenarios() -> list[tuple[str, list[tuple[str, str]], dict[str, Any]]]:
    """Load transcript/label pairs using the onboarding eval's mirrored layout."""
    scenarios: list[tuple[str, list[tuple[str, str]], dict[str, Any]]] = []
    transcript_paths = sorted(LIVE_DIR.glob("*.txt"))
    if not transcript_paths:
        raise FileNotFoundError(f"No live transcripts found in {LIVE_DIR}")

    for transcript_path in transcript_paths:
        expected_path = EXPECTED_DIR / f"{transcript_path.stem}.json"
        if not expected_path.exists():
            raise FileNotFoundError(f"Missing expected output: {expected_path}")
        transcript = _parse_transcript(transcript_path)
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        _validate_scenario(transcript_path.stem, transcript, expected)
        scenarios.append((transcript_path.stem, transcript, expected))
    return scenarios


def _parse_transcript(path: Path) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        if ":" not in raw:
            raise ValueError(f"{path}:{line_number}: expected 'Speaker: utterance'")
        speaker, text = raw.split(":", 1)
        if not speaker.strip() or not text.strip():
            raise ValueError(f"{path}:{line_number}: speaker and utterance are required")
        turns.append((speaker.strip(), text.strip()))
    return turns


def _validate_scenario(
    name: str, transcript: list[tuple[str, str]], expected: dict[str, Any]
) -> None:
    labels = expected.get("turns", [])
    if expected.get("scenario") != name:
        raise ValueError(f"{name}: expected scenario field to match filename")
    if len(transcript) != len(labels):
        raise ValueError(
            f"{name}: transcript has {len(transcript)} turns, labels have {len(labels)}"
        )
    roster = expected.get("roster")
    if not isinstance(roster, list) or not roster:
        raise ValueError(f"{name}: roster must be a non-empty list")
    for index, ((speaker, _text), label) in enumerate(zip(transcript, labels), 1):
        if label.get("speaker") != speaker:
            raise ValueError(
                f"{name} turn {index}: transcript speaker {speaker!r} does not match "
                f"label {label.get('speaker')!r}"
            )
        missing = [dimension for dimension in DIMENSIONS if dimension not in label]
        if missing:
            raise ValueError(f"{name} turn {index}: missing dimensions {missing}")


def evaluate_scenario(
    name: str, transcript: list[tuple[str, str]], expected: dict[str, Any]
) -> list[Observation]:
    avatar = avatars.load("laura")
    roster = list(expected["roster"])
    templates = meeting_state.templates_for(avatar)
    state = meeting_state.MeetingState()
    intervention_emitted = False
    observations: list[Observation] = []

    for (speaker, text), labels in zip(transcript, expected["turns"]):
        turn_id = str(labels["id"])
        xfails = labels.get("xfail", {})

        completeness = end_of_turn.completeness(text)
        floor_state = "hold" if completeness <= 0.3 else "yield"
        floor_cfg = labels["floor_holding"]
        wait = decision.adaptive_deference_seconds(
            DEFERENCE_BASE,
            enabled=True,
            lo=DEFERENCE_LO,
            hi=DEFERENCE_HI,
            since_partial=(0.0 if floor_cfg.get("active_partial") else 99.0),
            active_partial_seconds=ACTIVE_PARTIAL_SECONDS,
            n_humans=len(roster),
            is_question=text.rstrip().endswith("?"),
            turn_completeness=completeness,
        )
        floor_actual = f"{floor_state}:{_wait_bucket(wait)}"
        floor_expected = (
            f"{floor_cfg['expected']}:{floor_cfg['deference']}"
        )

        called, question = decision.detect_wake(avatar, text, roster)
        addressed_other = decision.addressed_to_other(text, roster)
        confidence = decision.passes_confidence(
            avatar, labels["address_detection"]["model_result"]
        )
        command_text = question if called else text
        stop = decision.detect_stop_command(command_text)
        leave = decision.detect_leave_command(command_text)
        closing = decision.detect_closing(text)
        invite_detector = getattr(decision, "detect_invite", None)
        invite = bool(invite_detector(command_text)) if callable(invite_detector) else False

        if called and stop:
            control_actual = "stop"
        elif called and leave:
            control_actual = "leave"
        elif invite:
            control_actual = "invite"
        elif closing:
            control_actual = "closing"
        else:
            control_actual = "none"

        control_suppresses_answer = control_actual in {"stop", "leave"}
        address_actual = (
            "speak"
            if confidence
            and not control_suppresses_answer
            and not (addressed_other and not called)
            else "silent"
        )
        target_actual = _resolve_target(
            avatar.name, text, roster, called=called, addressed_other=addressed_other
        )

        emotion_label = emotion.classify(text)
        emotion_actual = (
            f"{emotion_label}|{emotion.talk_mood(emotion_label)}|"
            f"{emotion.ditto_emo(emotion_label)}"
        )
        emotion_expected_cfg = labels["emotional_appropriateness"]
        emotion_expected = (
            f"{emotion_expected_cfg['label']}|{emotion_expected_cfg['talk_mood']}|"
            f"{emotion_expected_cfg['ditto_emo']}"
        )

        meeting_state.update(
            state,
            speaker,
            text,
            templates=templates,
            wake_words=avatar.wake_words,
        )
        # Drive the private live seam explicitly as requested. update() also calls
        # it; this second call verifies that evaluation is deterministic/idempotent.
        meeting_state._evaluate_intervention(state)
        intervention_text = meeting_state.intervention_line(state)
        if state.should_intervene and intervention_text and not intervention_emitted:
            intervention_actual = "nudge"
            intervention_emitted = True
        else:
            intervention_actual = "silent"

        actual_by_dimension = {
            "floor_holding": floor_actual,
            "address_detection": address_actual,
            "multiparty_deference": target_actual,
            "control_intent": control_actual,
            "emotional_appropriateness": emotion_actual,
            "intervention_timing": intervention_actual,
        }
        expected_by_dimension = {
            "floor_holding": floor_expected,
            "address_detection": labels["address_detection"]["expected"],
            "multiparty_deference": labels["multiparty_deference"]["expected"],
            "control_intent": labels["control_intent"]["expected"],
            "emotional_appropriateness": emotion_expected,
            "intervention_timing": labels["intervention_timing"]["expected"],
        }
        for dimension in DIMENSIONS:
            observations.append(
                Observation(
                    scenario=name,
                    turn_id=turn_id,
                    dimension=dimension,
                    expected=expected_by_dimension[dimension],
                    actual=actual_by_dimension[dimension],
                    xfail_reason=str(xfails.get(dimension, "")),
                )
            )
    return observations


def _wait_bucket(wait: float) -> str:
    if abs(wait - DEFERENCE_HI) < 1e-9:
        return "long"
    if abs(wait - DEFERENCE_LO) < 1e-9:
        return "short"
    if abs(wait - DEFERENCE_BASE) < 1e-9:
        return "base"
    return "medium"


def _resolve_target(
    avatar_name: str,
    text: str,
    roster: list[str],
    *,
    called: bool,
    addressed_other: bool,
) -> str:
    if called:
        return avatar_name.lower()
    if addressed_other:
        for candidate in _vocative_candidates(text):
            for name in roster:
                first = name.split()[0]
                if decision.fuzzy_name_match(candidate, first):
                    return name
        return "other"
    if text.rstrip().endswith("?"):
        return "room"
    return "none"


def _vocative_candidates(text: str) -> list[str]:
    lower = text.lower()
    candidates = (
        re.findall(r"(?:^|[,.;:!?]\s+)([a-z]+)\s*[,:]", lower)
        + re.findall(
            r"\b(?:hey|hi|hello|ok|okay|yo|ehi|ciao|senti|scusa|allora)\s+([a-z]+)\b",
            lower,
        )
        + re.findall(r",\s*([a-z]+)[^a-z]*$", lower)
    )
    return list(dict.fromkeys(candidates))


def run_all() -> EvalReport:
    scenarios = load_scenarios()
    observations = [
        observation
        for name, transcript, expected in scenarios
        for observation in evaluate_scenario(name, transcript, expected)
    ]
    scores = {
        dimension: _score_dimension(
            dimension,
            [
                o
                for o in observations
                if o.dimension == dimension and (not o.xfail_reason or o.correct)
            ],
        )
        for dimension in DIMENSIONS
    }
    realism = round(
        100.0 * sum(score.weight * score.score for score in scores.values()), 2
    )
    scored = [o for o in observations if not o.xfail_reason or o.correct]
    false_speech = sum(
        o.dimension == "address_detection"
        and o.expected == "silent"
        and o.actual == "speak"
        for o in scored
    )
    wrong_target = sum(
        o.dimension == "multiparty_deference"
        and o.expected not in {"none", "room"}
        and o.actual != o.expected
        for o in scored
    )
    xfails = [
        {
            "scenario": o.scenario,
            "turn_id": o.turn_id,
            "dimension": o.dimension,
            "reason": o.xfail_reason,
            "expected": o.expected,
            "actual": o.actual,
        }
        for o in observations
        if o.xfail_reason and not o.correct
    ]
    unexpected = [
        {
            "scenario": o.scenario,
            "turn_id": o.turn_id,
            "dimension": o.dimension,
            "expected": o.expected,
            "actual": o.actual,
        }
        for o in observations
        if not o.correct and not o.xfail_reason
    ]
    return EvalReport(
        scenarios=len(scenarios),
        turns=sum(len(transcript) for _name, transcript, _expected in scenarios),
        dimensions=scores,
        realism_score=realism,
        false_speech=false_speech,
        wrong_target=wrong_target,
        xfails=xfails,
        unexpected_mismatches=unexpected,
    )


def _score_dimension(dimension: str, observations: list[Observation]) -> DimensionScore:
    if not observations:
        raise ValueError(f"{dimension}: no scored observations")
    negative = NEGATIVE_LABEL[dimension]
    true_positive = false_positive = false_negative = 0
    correct = 0
    for observation in observations:
        if observation.correct:
            correct += 1
            if negative is None or observation.expected != negative:
                true_positive += 1
            continue
        if negative is None or observation.actual != negative:
            false_positive += 1
        if negative is None or observation.expected != negative:
            false_negative += 1

    precision = _safe_ratio(true_positive, true_positive + false_positive)
    recall = _safe_ratio(true_positive, true_positive + false_negative)
    beta = DIMENSION_BETA[dimension]
    beta_sq = beta * beta
    denominator = beta_sq * precision + recall
    f_score = (
        (1.0 + beta_sq) * precision * recall / denominator
        if denominator
        else 0.0
    )
    return DimensionScore(
        precision=round(precision, 4),
        recall=round(recall, 4),
        score=round(f_score, 4),
        correct=correct,
        total=len(observations),
        weight=DIMENSION_WEIGHTS[dimension],
    )


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def print_summary(report: EvalReport) -> None:
    print(
        "Laura live realism eval: "
        f"scenarios={report.scenarios}, turns={report.turns}, "
        f"realism={report.realism_score:.2f}/100, baseline={BASELINE_THRESHOLD:.2f}"
    )
    print("\nPer-dimension scorecard")
    for name, score in report.dimensions.items():
        print(
            f"  {name:29} P={score.precision:.3f} R={score.recall:.3f} "
            f"score={score.score:.3f} weight={score.weight:.2f} "
            f"correct={score.correct}/{score.total}"
        )
    print(
        "\nSafety errors: "
        f"false_speech={report.false_speech}, wrong_target={report.wrong_target}"
    )

    if report.xfails:
        print("\nIntentional xfails (known runtime findings)")
        for finding in report.xfails:
            print(
                f"  XFAIL {finding['scenario']}:{finding['turn_id']} "
                f"[{finding['dimension']}] {finding['reason']} "
                f"(expected={finding['expected']}, actual={finding['actual']})"
            )
    if report.unexpected_mismatches:
        print("\nUnexpected mismatches")
        for mismatch in report.unexpected_mismatches:
            print(
                f"  FAIL {mismatch['scenario']}:{mismatch['turn_id']} "
                f"[{mismatch['dimension']}] expected={mismatch['expected']}, "
                f"actual={mismatch['actual']}"
            )

    print("\nLIVE_REALISM_JSON=" + json.dumps(report.to_dict(), sort_keys=True))


def main() -> int:
    report = run_all()
    print_summary(report)
    return int(
        report.realism_score < BASELINE_THRESHOLD
        or bool(report.unexpected_mismatches)
    )


if __name__ == "__main__":
    raise SystemExit(main())
