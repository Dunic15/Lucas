"""VisualPlanner (B0); one provider-independent planning interface.

Contract: ``observe → propose ONE operation``. The planner is a SUGGESTER;
the deterministic policy engine (policy.py) remains the sole authority; a
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

# Stable proposal shape; additive changes only. (B1 supersets these fields via
# contracts.visual_proposal: target_type, coordinates, observed_observation_id.)
PROPOSAL_FIELDS = (
    "operation", "target", "arguments", "reason", "confidence",
    "expected_result", "observed_page_version", "consequential",
)

# Operations a proposal may carry; a UNION (B0's observe/done kept; B1 verbs
# added). Loop-control ops (finish/request_human_help) are handled by the
# coordinator and NEVER sent to the operator.
OPERATIONS = ("observe", "navigate", "click", "type", "scroll", "done",
              "wait", "inspect", "go_back", "finish", "request_human_help")


def proposal(
    *, operation: str, target: str = "", arguments: dict | None = None,
    reason: str = "", confidence: float = 0.5,
    expected_result: dict | None = None, observed_page_version: int = 0,
    consequential: bool = False,
) -> dict:
    """Build a well-formed B0-shaped planner proposal (still valid in B1)."""
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
    """observe → propose one operation (or None when done/stuck).

    B1 APPENDS keyword-only params (never reorders): B0 callers keep using
    ``propose(observation, goal)`` unchanged; the coordinator passes the rest
    by keyword. A planner may ignore any of them."""

    name: str

    def propose(self, observation: dict, goal: str, *,
                previous_result: Optional[dict] = None,
                allowed_operations: Optional[tuple] = None,
                budget: Optional[dict] = None,
                screenshot: Optional[bytes] = None) -> Optional[dict]:
        ...


class ScriptedPlanner:
    """Deterministic B0 planner: walks a fixed script of proposals, validating
    each against the CURRENT observation (element must exist; page version
    must match what the step expects); so a stale page yields a REPLAN
    (re-propose from the fresh observation) instead of a blind action.
    """

    name = "scripted"

    def __init__(self, script: list[dict]):
        self._script = [dict(s) for s in script]
        self._index = 0

    def propose(self, observation: dict, goal: str, **_kw) -> Optional[dict]:
        if self._index >= len(self._script):
            return None
        step = self._script[self._index]
        op = str(step.get("operation") or "")
        target = str(step.get("target") or "")
        # Ground the proposal in the live observation: a targeted operation
        # whose element is absent from the CURRENT page is not proposed -
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


class FakeVisualPlanner:
    """Deterministic, network-free VISUAL planner for CI + fixtures + demos.

    Unlike ScriptedPlanner it grounds targets from the SCREENSHOT perception
    (the fake screenshot bytes carry a ``primary_visual_target`` + bboxes that
    the DOM text cannot disambiguate; proving real "eyes"), and it can emit
    every B1 scenario a coordinator must handle. Emits contracts.visual_proposal
    shapes. NEVER a network/model call."""

    name = "fake-visual"

    # Scenario knobs (default = a well-formed visual selection).
    def __init__(self, *, mode: str = "visual_select",
                 script: Optional[list[dict]] = None):
        self._mode = mode
        self._script = [dict(s) for s in (script or [])]
        self._i = 0

    def propose(self, observation: dict, goal: str, *,
                previous_result: Optional[dict] = None,
                allowed_operations: Optional[tuple] = None,
                budget: Optional[dict] = None,
                screenshot: Optional[bytes] = None) -> Optional[dict]:
        from . import contracts

        pv = int(observation.get("page_version") or 0)
        oid = str(observation.get("observation_id") or "")
        perception = _decode_screenshot(screenshot)

        if self._mode == "script":
            return self._from_script(observation, pv, oid)
        if self._mode == "malformed":
            return {"operation": "click", "surprise_key": "smuggle",
                    "target": "x"}  # extra key → validator rejects
        if self._mode == "unknown_op":
            return contracts.visual_proposal(operation="exfiltrate",
                                             observed_page_version=pv)
        if self._mode == "low_confidence":
            tgt = perception.get("primary_visual_target") or ""
            bbox = perception.get("bbox_for", {}).get(tgt)
            return contracts.visual_proposal(
                operation="click", target_type="coordinates",
                coordinates=_center(bbox), confidence=0.2,
                reason="unsure", observed_page_version=pv,
                observed_observation_id=oid)
        if self._mode == "wrong_target":
            # Deliberately propose a decoy target present in the DOM.
            return contracts.visual_proposal(
                operation="click", target_type="element",
                target="continue-left", confidence=0.9,
                reason="(wrong) left option", observed_page_version=pv,
                observed_observation_id=oid,
                expected_result={"url_contains": "pricing"})
        if self._mode == "stale":
            # Reference an OLD observation id → coordinator rejects as stale.
            return contracts.visual_proposal(
                operation="click", target_type="element", target="buy-team",
                observed_page_version=max(0, pv - 5),
                observed_observation_id="obs:stale:0", confidence=0.9)
        if self._mode == "guarded":
            return contracts.visual_proposal(
                operation="click", target_type="element", target="buy-team",
                consequential=True, confidence=0.9,
                reason="purchase", observed_page_version=pv,
                observed_observation_id=oid)
        if self._mode == "blocked":
            return contracts.visual_proposal(
                operation="type", target_type="element", target="pass",
                arguments={"text": "hunter2"}, confidence=0.9,
                observed_page_version=pv, observed_observation_id=oid)
        if self._mode == "finish":
            return contracts.visual_proposal(operation="finish",
                                             reason="goal met",
                                             observed_page_version=pv)
        # default: visual_select; pick the screenshot-only primary target by
        # COORDINATES (the DOM text is ambiguous by construction).
        tgt = perception.get("primary_visual_target") or ""
        bbox = perception.get("bbox_for", {}).get(tgt)
        if bbox:
            return contracts.visual_proposal(
                operation="click", target_type="coordinates",
                coordinates=_center(bbox), confidence=0.95,
                reason=f"visual primary target {tgt}",
                observed_page_version=pv, observed_observation_id=oid,
                expected_result={"url_contains": "pricing"})
        # No screenshot perception ⇒ cannot ground visually ⇒ ask for help.
        return contracts.visual_proposal(
            operation="request_human_help", reason="no visual grounding",
            observed_page_version=pv, observed_observation_id=oid)

    def _from_script(self, observation, pv, oid):
        from . import contracts

        if self._i >= len(self._script):
            return contracts.visual_proposal(operation="finish",
                                             observed_page_version=pv)
        step = self._script[self._i]
        self._i += 1
        return contracts.visual_proposal(
            operation=str(step.get("operation") or "observe"),
            target_type=str(step.get("target_type") or "none"),
            target=str(step.get("target") or ""),
            coordinates=step.get("coordinates"),
            arguments=step.get("arguments") if isinstance(
                step.get("arguments"), dict) else None,
            reason=str(step.get("reason") or ""),
            confidence=float(step.get("confidence", 0.9)),
            expected_result=step.get("expected_result") if isinstance(
                step.get("expected_result"), dict) else None,
            observed_page_version=pv, observed_observation_id=oid,
            consequential=bool(step.get("consequential", False)))


def _decode_screenshot(screenshot: Optional[bytes]) -> dict:
    """Decode the fake provider's deterministic screenshot payload into the
    visual perception a real vision model would return. NEVER used for real
    image bytes (the real planner sends them to the model)."""
    import json as _json

    if not screenshot:
        return {}
    try:
        data = _json.loads(screenshot.decode())
    except (ValueError, UnicodeDecodeError):
        return {}
    bbox_for = {e.get("id"): e.get("bbox") for e in data.get("elements") or []
                if e.get("bbox")}
    return {"primary_visual_target": data.get("primary_visual_target", ""),
            "bbox_for": bbox_for}


def _center(bbox) -> Optional[list]:
    if not (bbox and len(bbox) >= 4):
        return None
    return [int((bbox[0] + bbox[2]) / 2), int((bbox[1] + bbox[3]) / 2)]
