"""Perception coordinator (B1); the bounded observe→plan→act→verify loop.

Composed PURELY from operator.perceive (read) + operator.issue_command
(execute) + operator.get_session (state). It NEVER calls a provider, the DAL,
policy.classify, or mints/approves actions directly; every side effect goes
through issue_command, so per-step policy classification, guarded→approval
routing, idempotency, page_version binding, and operator-owned verification are
all inherited unchanged. There is NO second execution or approval system.

The loop is a SINGLE synchronous bounded run (never a daemon, never unbounded):
hard caps on steps, consecutive failures, replans, duration, and model calls,
plus session expiry and the navigation domain allowlist. A guarded step ends
the run as ``awaiting_approval`` (the run records the action_id and STOPS; it
never waits, polls, or self-approves; resumption is a fresh run that
re-observes and re-plans).

Flag-gated: with ``BROWSER_VISUAL_PLANNER_ENABLED`` off the coordinator is
inert (callers get ``disabled``); the fake planner is injectable for CI.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from . import contracts, operator, policy
from .planner import FakeVisualPlanner

# Planner-proposed operations the coordinator handles LOCALLY (never sent to
# issue_command). Everything else is a browser op dispatched via the operator.
_CONTROL_OPS = ("finish", "request_human_help", "done")


def _step_info(operation: str, proposal: dict, observation: dict) -> dict:
    """A BOUNDED, redacted description of the step for live narration: the
    operation plus the target control's role + short name resolved from the
    (already-sanitized) observation. Names are capped and redacted; element
    `value` is never in the observation, so no field contents leak."""
    from . import policy

    info = {"operation": operation, "role": "", "name": ""}
    target = str(proposal.get("target") or "")
    for el in (observation.get("elements") or []):
        if str(el.get("id")) == target:
            info["role"] = str(el.get("role") or "")[:20]
            name = str(el.get("name") or "").strip()
            info["name"] = policy.redact(name)[:40] if name else ""
            break
    return info


def run(
    org_id: str, session_id: str, goal: str, *, principal: str = "",
    planner: Any = None, clock: Optional[Callable[[], float]] = None,
    on_step: Optional[Callable[[int, str, dict], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Run the bounded perception loop toward ``goal``. Returns a transcript:
    {outcome, steps[], action_id?, replans, model_calls, reason}.

    outcome ∈ finished | awaiting_approval | budget_exhausted |
             stalled | expired | blocked | disabled | error.
    """
    from ..config import settings
    from .. import browser

    if not browser.enabled() or not settings.browser_visual_planner_enabled:
        return {"outcome": "disabled", "steps": [], "reason": "flag_off"}

    planner = planner or _real_planner()
    if planner is None:
        return {"outcome": "disabled", "steps": [],
                "reason": "planner_unconfigured"}

    now = clock or time.monotonic
    started = now()
    allowed_domains = policy.allowed_domain_set(settings.browser_allowed_domains)

    max_steps = int(settings.browser_coord_max_steps)
    max_fail = int(settings.browser_coord_max_consecutive_failures)
    max_replans = int(settings.browser_coord_max_replans)
    max_duration = float(settings.browser_coord_max_duration_seconds)
    max_model = int(settings.browser_coord_max_model_calls)

    steps: list[dict] = []
    consecutive_failures = 0
    replans = 0
    model_calls = 0

    def stop(outcome: str, reason: str = "", action_id: str = "") -> dict:
        return {"outcome": outcome, "reason": reason, "action_id": action_id,
                "steps": steps, "replans": replans, "model_calls": model_calls,
                "step_count": len(steps)}

    for step_index in range(max_steps):
        # Cancellation (e.g. a meeting barge-in / "stop") checked FIRST every
        # iteration, so a human interruption halts the walkthrough immediately
        #; the narration must never talk over someone who took the floor.
        if cancel is not None:
            try:
                if cancel():
                    return stop("cancelled", "cancelled")
            except Exception:  # noqa: BLE001; a cancel-check fault never runs away
                pass
        # Hard bounds re-checked EVERY iteration; the loop can never run away.
        if now() - started > max_duration:
            return stop("budget_exhausted", "max_duration")
        if model_calls >= max_model:
            return stop("budget_exhausted", "max_model_calls")

        # Session liveness (TTL is enforced lazily by the operator; a mid-run
        # expiry ends the loop).
        view = operator.get_session(org_id, session_id, principal=principal)
        if view is None:
            return stop("error", "session_not_found")
        if view["state"] not in ("ready", "presenting"):
            return stop("expired", f"session_{view['state']}")

        # PERCEIVE (read-only): byte-free observation + transient screenshot.
        perceived = operator.perceive(org_id, session_id, principal=principal)
        if perceived is None:
            return stop("error", "perceive_failed")
        observation, screenshot = perceived

        # PLAN: one proposal from the (untrusted-page-aware) planner.
        model_calls += 1
        try:
            raw_proposal = planner.propose(
                observation, goal, screenshot=screenshot,
                allowed_operations=contracts.ALL_OPERATIONS,
                budget={"steps_left": max_steps - step_index,
                        "model_calls_left": max_model - model_calls})
        except Exception:  # noqa: BLE001; planner failure is fail-closed
            raw_proposal = None
        # Drop the screenshot bytes immediately after planning; never persist.
        screenshot = b""

        if raw_proposal is None:
            consecutive_failures += 1
            steps.append({"step": step_index, "outcome": "planner_error"})
            if consecutive_failures >= max_fail:
                return stop("stalled", "planner_error")
            continue

        # VALIDATE the proposal schema (fail-closed on model output).
        verdict = contracts.validate_proposal(
            raw_proposal, viewport=observation.get("viewport"))
        if not verdict.get("ok"):
            consecutive_failures += 1
            steps.append({"step": step_index, "outcome": "invalid_proposal",
                          "reason": verdict.get("reason")})
            if consecutive_failures >= max_fail:
                return stop("stalled", "invalid_proposal")
            continue
        proposal = verdict["proposal"]
        op = proposal["operation"]

        # Live narration hook: fires BEFORE the action so the avatar's voice
        # leads the on-screen click. Best-effort; a narration fault must never
        # break the loop (and never blocks: the callback dispatches async). The
        # callback receives operation + proposal; callers build a BOUNDED phrase
        # from the operation only (never page content) to keep speech leak-free.
        if on_step is not None and op not in _CONTROL_OPS:
            # Re-check cancel right before narrating: a barge-in that landed
            # during perceive/plan must suppress this line, not speak it.
            if cancel is not None:
                try:
                    if cancel():
                        return stop("cancelled", "cancelled")
                except Exception:  # noqa: BLE001
                    pass
            try:
                on_step(step_index, op, _step_info(op, proposal, observation))
            except Exception:  # noqa: BLE001; narration never breaks the run
                pass

        # CONTROL ops handled locally; never dispatched.
        if op in _CONTROL_OPS:
            if op == "request_human_help":
                return stop("awaiting_approval", "request_human_help")
            return stop("finished", "planner_finished")

        # NAVIGATION domain gate (server-authoritative; page links only narrow).
        if op == "navigate":
            target = str(proposal.get("target")
                         or proposal.get("arguments", {}).get("url") or "")
            # The SERVER domain allowlist is the hard, page-independent gate.
            # (observation_links is available for stricter deployments that
            # additionally require the target to be an on-page link; it can
            # only NARROW, never widen; so it is not enforced here to keep
            # seed navigations to allowlisted hosts working.)
            nav = policy.check_navigation_target(target, allowed_domains)
            if not nav.get("ok"):
                consecutive_failures += 1
                steps.append({"step": step_index, "outcome": "domain_blocked",
                              "reason": nav.get("reason")})
                if consecutive_failures >= max_fail:
                    return stop("blocked", "domain_blocked")
                continue

        # EXECUTE through the ONE gated path. One proposal → one issue_command.
        result = _execute(org_id, session_id, principal, proposal, observation,
                          run_step=f"{started}:{step_index}")
        steps.append({"step": step_index, "operation": op,
                      "accepted": result.get("accepted"),
                      "classification": result.get("classification"),
                      "verification": result.get("verification"),
                      "failure_category": result.get("failure_category"),
                      "action_id": result.get("action_id") or ""})

        reason = str(result.get("reason") or "")
        # Guarded write → suspend the run for human approval (never poll/self-
        # approve). This keeps the M0 approve door the sole executor.
        if reason == "approval_required" or result.get("action_id"):
            return stop("awaiting_approval", "guarded_step",
                        action_id=str(result.get("action_id") or ""))
        # A guarded write rejected under the read-only posture also suspends.
        if result.get("failure_category") == "write_rejected_read_only":
            return stop("awaiting_approval", "write_rejected_read_only")

        if result.get("accepted"):
            consecutive_failures = 0
            # Success + verified/inconclusive continues; the planner decides
            # when the goal is met (a subsequent 'finish').
            continue

        # A failure: recoverable ⇒ replan (bounded), terminal ⇒ stop.
        consecutive_failures += 1
        if result.get("replanning_permitted"):
            replans += 1
            if replans > max_replans:
                return stop("stalled", "max_replans")
            if consecutive_failures >= max_fail:
                return stop("stalled", "consecutive_failures")
            continue
        return stop("blocked", reason or "terminal_failure")

    return stop("budget_exhausted", "max_steps")


def _execute(org_id, session_id, principal, proposal, observation,
             run_step) -> dict:
    """Map a validated proposal to exactly ONE issue_command. Verification is
    delegated to the operator (verify=True); the planner never self-certifies.
    """
    op = proposal["operation"]
    command_id = f"coord:{run_step}"
    kwargs: dict = {
        "verb": op, "principal": principal, "command_id": command_id,
        "verify": True, "expected": proposal.get("expected_result") or {},
        "confidence": proposal.get("confidence"),
        # STALE ANCHOR: the SERVER-perceived page_version (from perceive()),
        # NOT the model's echoed observed_page_version; an injected page/model
        # cannot defeat the stale gate by reporting a fresh integer.
        "observed_page_version": int(observation.get("page_version") or 0),
    }
    if proposal.get("target_type") == "coordinates":
        kwargs["coordinates"] = proposal.get("coordinates")
    elif proposal.get("target_type") == "element":
        kwargs["element_id"] = proposal.get("target")
    if op == "navigate":
        kwargs["url"] = str(proposal.get("target")
                            or proposal.get("arguments", {}).get("url") or "")
    if op == "type":
        kwargs["text"] = str(proposal.get("arguments", {}).get("text") or "")
    return operator.issue_command(org_id, session_id, **kwargs)


def _observation_link_urls(observation: dict) -> set[str]:
    """URLs that appear as links in the CURRENT observation; used only to
    NARROW navigation (a target must be a real on-page link), never to widen
    the server allowlist. The fake provider carries hrefs on link elements."""
    out: set[str] = set()
    for el in observation.get("elements") or []:
        href = el.get("href")
        if isinstance(href, str) and href:
            out.add(href)
    return out


def _real_planner():
    from .multimodal import get_visual_planner

    return get_visual_planner()


def new_fake(**kwargs) -> FakeVisualPlanner:
    """Test/demo helper to inject a deterministic planner into run()."""
    return FakeVisualPlanner(**kwargs)
