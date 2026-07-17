"""VisualPlanner (B0) — one provider-independent planning interface.

Contract: ``observe → propose ONE operation``. The planner is a SUGGESTER;
the deterministic policy engine (policy.py) remains the sole authority — a
proposal's ``consequential`` field is a suggestion the policy re-derives and
can override, and a proposal can never widen an action class or skip a check.
The planner also never declares its own operation successful: verification is
the operator's post-operation observation compare (contracts.verify_expectation).

B0 ships ONE implementation: a deterministic scripted planner for tests,
fixtures and demos (no model, no network). The real multimodal planner
(screenshot → OpenAI computer-use) is B1+ and slots behind this same
interface; provider/model identifiers never become part of the proposal shape.
"""
from __future__ import annotations

from typing import Optional, Protocol

# Stable proposal shape — additive changes only.
PROPOSAL_FIELDS = (
    "operation", "target", "arguments", "reason", "confidence",
    "expected_result", "observed_page_version", "consequential",
)

# Operations a proposal may carry (mirrors the operator's verbs).
OPERATIONS = ("observe", "navigate", "click", "type", "scroll", "done")


def proposal(
    *, operation: str, target: str = "", arguments: dict | None = None,
    reason: str = "", confidence: float = 0.5,
    expected_result: dict | None = None, observed_page_version: int = 0,
    consequential: bool = False,
) -> dict:
    """Build a well-formed planner proposal."""
    return {
        "operation": operation,
        "target": target,
        "arguments": dict(arguments or {}),
        "reason": str(reason)[:300],
        "confidence": max(0.0, min(float(confidence), 1.0)),
        "expected_result": dict(expected_result or {}),
        "observed_page_version": int(observed_page_version),
        "consequential": bool(consequential),
    }


class VisualPlanner(Protocol):
    """observe → propose one operation (or None when done/stuck)."""

    name: str

    def propose(self, observation: dict, goal: str) -> Optional[dict]:
        ...


class ScriptedPlanner:
    """Deterministic B0 planner: walks a fixed script of proposals, validating
    each against the CURRENT observation (element must exist; page version
    must match what the step expects) — so a stale page yields a REPLAN
    (re-propose from the fresh observation) instead of a blind action.
    """

    name = "scripted"

    def __init__(self, script: list[dict]):
        self._script = [dict(s) for s in script]
        self._index = 0

    def propose(self, observation: dict, goal: str) -> Optional[dict]:
        if self._index >= len(self._script):
            return None
        step = self._script[self._index]
        op = str(step.get("operation") or "")
        target = str(step.get("target") or "")
        # Ground the proposal in the live observation: a targeted operation
        # whose element is absent from the CURRENT page is not proposed —
        # the planner re-observes (stale page → replan, never a blind click).
        if op in ("click", "type") and target:
            ids = {str(e.get("id")) for e in observation.get("elements") or []}
            if target not in ids:
                return proposal(
                    operation="observe", reason="stale page: replanning",
                    observed_page_version=int(
                        observation.get("page_version") or 0),
                    confidence=0.3,
                )
        self._index += 1
        return proposal(
            operation=op,
            target=target,
            arguments=dict(step.get("arguments") or {}),
            reason=str(step.get("reason") or f"scripted step {self._index}"),
            confidence=float(step.get("confidence", 0.9)),
            expected_result=dict(step.get("expected_result") or {}),
            observed_page_version=int(observation.get("page_version") or 0),
            consequential=bool(step.get("consequential", False)),
        )
