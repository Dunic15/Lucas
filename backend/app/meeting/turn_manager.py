"""Deterministic social turn policy built on ConversationFrame snapshots.

The manager never reads or stores transcript text. It consumes only the stable,
PII-safe snapshot emitted by conversation_frame plus timing/classification
signals already computed by the live path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


_VALID_MODES = frozenset({"off", "shadow", "on"})


@dataclass(frozen=True)
class TurnPlan:
    """One PII-safe recommendation for the current finalized utterance."""

    policy: str
    skip_deference: bool = False
    suppress_interjection: bool = False


def normalize_mode(value: Any) -> str:
    """Fail closed: unknown rollout values behave as shadow, never as on."""

    mode = str(value or "").strip().lower()
    return mode if mode in _VALID_MODES else "shadow"


def is_active(value: Any) -> bool:
    return normalize_mode(value) == "on"


def plan_turn(
    snapshot: Mapping[str, Any],
    *,
    called: bool,
    followup: bool,
    turn_completeness: float,
    locked_dyad: bool = False,
) -> TurnPlan:
    """Plan etiquette without generating text or causing side effects.

    The first live rollout is intentionally suppression/latency-only:
    direct asks and engaged follow-ups retain today's behaviour; a clearly
    finished question in a true 1:1 can skip the human-deference sleep; and a
    rapid two-human exchange suppresses unprompted spoken interjections.
    """

    if called:
        return TurnPlan("direct_address")
    if followup:
        return TurnPlan("engaged_followup")
    if snapshot.get("addressed_to") == "participant":
        return TurnPlan("yield_to_participant", suppress_interjection=True)
    if locked_dyad:
        return TurnPlan("locked_dyad", suppress_interjection=True)

    meeting_size = str(snapshot.get("meeting_size") or "empty")
    open_question = bool(snapshot.get("open_question"))
    if (
        meeting_size == "one_to_one"
        and open_question
        and float(turn_completeness) >= 0.60
    ):
        return TurnPlan("one_to_one_fast", skip_deference=True)
    if meeting_size == "large_group":
        return TurnPlan("large_group", suppress_interjection=True)
    if meeting_size == "small_group":
        return TurnPlan("small_group")
    return TurnPlan("observe")
