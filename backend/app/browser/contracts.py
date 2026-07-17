"""Stable typed contracts for the Browser B0 demo handoff.

These are the shapes the demo integrator and the Control Center V2 design
branch code against. They are provider-independent and secret-free by
construction — the operator builds them; nothing here calls a provider.

Field names are STABLE: additive changes only. Everything is a plain dict
(JSON-serializable, no Decimal) so the router can return it directly.
"""
from __future__ import annotations

from typing import Any

# ── BrowserObservation ──────────────────────────────────────────────────────
# The bounded, sanitized page read handed to the agent/frontend. Built from a
# provider RawObservation via policy.sanitize_observation plus session-scoped
# fields (session_id, command_sequence, page_version, timestamp).

OBSERVATION_FIELDS = (
    "session_id", "command_sequence", "page_version", "url", "title",
    "viewport", "screenshot_ref", "dom_summary", "visible_text", "elements",
    "truncated", "timestamp",
)


def build_observation(
    *, session_id: str, command_sequence: int, page_version: int,
    sanitized: dict, timestamp: float, viewport: dict | None = None,
) -> dict[str, Any]:
    """Compose the stable BrowserObservation. ``sanitized`` is the output of
    policy.sanitize_observation (already redacted + bounded)."""
    dom = str(sanitized.get("dom_summary") or "")
    return {
        "session_id": session_id,
        "command_sequence": int(command_sequence),
        "page_version": int(page_version),
        "url": sanitized.get("url", ""),
        "title": sanitized.get("title", ""),
        "viewport": dict(viewport or {"width": 1280, "height": 720}),
        "screenshot_ref": sanitized.get("screenshot_ref", ""),
        "dom_summary": dom,
        # A visible-text summary distinct from the structural DOM summary; both
        # are already secret-redacted. Bounded.
        "visible_text": dom[:1200],
        "elements": sanitized.get("elements", []),
        "truncated": bool(sanitized.get("truncated", False)),
        "timestamp": float(timestamp),
    }


# ── Browser command result ──────────────────────────────────────────────────
# Every issue_command response conforms to this shape.

FAILURE_CATEGORIES = (
    "", "not_found", "not_owner", "invalid_state", "browser_tool_not_allowed",
    "blocked", "provider_error", "provider_unconfigured", "execution_unknown",
    "unknown_verb", "write_rejected_read_only", "verification_failed",
)


def command_result(
    *, accepted: bool, command_sequence: int, page_version: int,
    classification: str, observation: dict | None = None,
    observation_ref: str = "", action_id: str = "",
    failure_category: str = "", replanning_permitted: bool = True,
    verification: str = "", extra: dict | None = None,
) -> dict[str, Any]:
    """The stable command-result contract.

    - ``accepted``: the command was executed (or legitimately routed to
      approval); ``False`` means rejected/failed.
    - ``classification``: policy class (auto|guarded|blocked|"").
    - ``action_id``: present iff a canonical approval was minted.
    - ``failure_category``: one of FAILURE_CATEGORIES ("" on success).
    - ``replanning_permitted``: may the planner propose again? False on a
      terminal/ownership failure; True on a recoverable one (e.g. stale page).
    - ``verification``: "", verified, not_verified, inconclusive.
    """
    result = {
        "accepted": bool(accepted),
        "command_sequence": int(command_sequence),
        "page_version": int(page_version),
        "classification": classification,
        "observation_ref": observation_ref,
        "action_id": action_id,
        "failure_category": failure_category,
        "replanning_permitted": bool(replanning_permitted),
        "verification": verification,
    }
    if observation is not None:
        result["observation"] = observation
    if extra:
        result.update(extra)
    return result


# ── Visual verification verdicts ────────────────────────────────────────────

VERIFIED = "verified"
NOT_VERIFIED = "not_verified"
INCONCLUSIVE = "inconclusive"


def verify_expectation(expected: dict, observation: dict) -> str:
    """Deterministic post-operation visual verification (B0).

    Compares an ``expected`` result descriptor against the NEW observation the
    operator captured AFTER executing. The planner NEVER declares its own
    success — this function does, from the fresh observation. Returns
    verified / not_verified / inconclusive.

    Supported expectation keys (all optional; absent ⇒ inconclusive):
      url_contains, title_contains, text_contains, element_present (id),
      page_version_at_least
    """
    checks = []
    url = str(observation.get("url") or "")
    title = str(observation.get("title") or "")
    text = str(observation.get("visible_text")
               or observation.get("dom_summary") or "")
    element_ids = {str(e.get("id")) for e in observation.get("elements") or []}

    if "url_contains" in expected:
        checks.append(str(expected["url_contains"]) in url)
    if "title_contains" in expected:
        checks.append(str(expected["title_contains"]).lower() in title.lower())
    if "text_contains" in expected:
        checks.append(str(expected["text_contains"]).lower() in text.lower())
    if "element_present" in expected:
        checks.append(str(expected["element_present"]) in element_ids)
    if "page_version_at_least" in expected:
        checks.append(
            int(observation.get("page_version") or 0)
            >= int(expected["page_version_at_least"]))

    if not checks:
        return INCONCLUSIVE
    return VERIFIED if all(checks) else NOT_VERIFIED


# ── Demo-run metadata (bounded, non-authoritative) ──────────────────────────

METADATA_FIELDS = (
    "demo_definition_id", "demo_definition_version", "demo_run_id",
    "current_checkpoint",
)


def clean_metadata(raw: dict | None) -> dict[str, str]:
    """Whitelist + bound the demo-run metadata. It is a label the integrator
    carries through; it NEVER influences state, policy, or the provider."""
    raw = raw or {}
    out: dict[str, str] = {}
    for key in METADATA_FIELDS:
        value = raw.get(key)
        if value is not None:
            out[key] = str(value)[:120]
    return out
