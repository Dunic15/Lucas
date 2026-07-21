"""Stable typed contracts for the Browser B0 demo handoff.

These are the shapes the demo integrator and the Control Center V2 design
branch code against. They are provider-independent and secret-free by
construction; the operator builds them; nothing here calls a provider.

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
    # B1 additive fields (all backward-compatible; legacy callers ignore them):
    "observation_id", "org_id", "principal", "a11y_summary",
    "screenshot_digest", "screenshot_bytes_len",
)


def observation_id(session_id: str, command_sequence: int) -> str:
    """Stable per-observation id, deterministic from (session, seq). No new
    table; the id binds the in-flight observation object; a proposal that
    references a superseded id is stale."""
    return f"obs:{session_id}:{int(command_sequence)}"


def _assert_byte_free(value, field: str) -> None:
    """A BrowserObservation must never carry raw image bytes or an inline
    data: image; screenshots leave only as a ref + digest. Fail loud in dev
    if a byte-bearing value ever reaches the contract boundary."""
    if isinstance(value, (bytes, bytearray)):
        raise ValueError(f"observation field {field!r} carries raw bytes")
    if isinstance(value, str) and value[:11].lower().startswith("data:image"):
        raise ValueError(f"observation field {field!r} carries an inline image")


def build_observation(
    *, session_id: str, command_sequence: int, page_version: int,
    sanitized: dict, timestamp: float, viewport: dict | None = None,
    org_id: str = "", principal: str = "",
) -> dict[str, Any]:
    """Compose the stable BrowserObservation. ``sanitized`` is the output of
    policy.sanitize_observation (already redacted, bounded, and BYTE-FREE -
    only a screenshot_ref + digest, never image bytes). B1 binds the
    observation to org/principal and gives it a stable id for stale detection.
    """
    dom = str(sanitized.get("dom_summary") or "")
    screenshot_ref = sanitized.get("screenshot_ref", "")
    _assert_byte_free(screenshot_ref, "screenshot_ref")
    return {
        "session_id": session_id,
        "command_sequence": int(command_sequence),
        "page_version": int(page_version),
        "observation_id": observation_id(session_id, command_sequence),
        "org_id": org_id,
        "principal": principal,
        "url": sanitized.get("url", ""),
        "title": sanitized.get("title", ""),
        "viewport": dict(viewport or sanitized.get("viewport")
                         or {"width": 1280, "height": 720}),
        "screenshot_ref": screenshot_ref,
        "screenshot_digest": sanitized.get("screenshot_digest", ""),
        "screenshot_bytes_len": int(sanitized.get("screenshot_bytes_len") or 0),
        "dom_summary": dom,
        # A11y-tree distillation, DISTINCT from the free-text dom_summary.
        "a11y_summary": str(sanitized.get("a11y_summary") or ""),
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
    # B1 additive categories:
    "domain_blocked", "coordinate_out_of_viewport", "stale_observation",
    "low_confidence", "planner_error", "coordinate_unresolved",
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
    success; this function does, from the fresh observation. Returns
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


# ── VisualProposal + strict schema validation (B1) ──────────────────────────
# The model proposes ONE typed operation; this validator is the fail-closed
# gate on model output. A proposal that is malformed, carries an unknown
# operation/extra keys, or out-of-range coordinates is REJECTED; the model
# can never smuggle intent through the schema. operation/consequential/
# confidence are SUGGESTIONS; the deterministic policy re-derives the class.

# Operations the coordinator understands. Split into loop-control (handled
# locally, NEVER sent to the operator) and browser ops (dispatched via
# issue_command). 'observe'/'done' kept for B0 back-compat.
CONTROL_OPERATIONS = ("finish", "request_human_help", "done")
BROWSER_OPERATIONS = ("navigate", "click", "type", "scroll", "wait",
                      "inspect", "go_back", "observe")
ALL_OPERATIONS = BROWSER_OPERATIONS + ("finish", "request_human_help")

TARGET_TYPES = ("element", "coordinates", "url", "none")

_PROPOSAL_KEYS = {
    "operation", "target_type", "target", "coordinates", "arguments",
    "reason", "confidence", "expected_result", "observed_page_version",
    "observed_observation_id", "consequential",
}


def visual_proposal(
    *, operation: str, target_type: str = "none", target: str = "",
    coordinates: tuple | list | None = None, arguments: dict | None = None,
    reason: str = "", confidence: float = 0.5,
    expected_result: dict | None = None, observed_page_version: int = 0,
    observed_observation_id: str = "", consequential: bool = False,
) -> dict[str, Any]:
    """Build a well-formed VisualProposal (the B1 superset of a B0 proposal).
    Coordinates are [x, y] in viewport pixels when target_type=='coordinates'.
    """
    coords = None
    if coordinates is not None and len(coordinates) >= 2:
        coords = [int(coordinates[0]), int(coordinates[1])]
    return {
        "operation": str(operation),
        "target_type": str(target_type),
        "target": str(target)[:200],
        "coordinates": coords,
        "arguments": dict(arguments or {}),
        "reason": str(reason)[:300],
        "confidence": max(0.0, min(float(confidence), 1.0)),
        "expected_result": dict(expected_result or {}),
        "observed_page_version": int(observed_page_version),
        "observed_observation_id": str(observed_observation_id)[:120],
        "consequential": bool(consequential),
    }


def validate_proposal(raw: Any, *, viewport: dict | None = None) -> dict:
    """Fail-closed schema validation of untrusted model output. Returns
    {ok, proposal|reason}. Rejects non-dicts, unknown/extra keys, unknown
    operations, malformed/out-of-viewport coordinates. NEVER raises."""
    if not isinstance(raw, dict):
        return {"ok": False, "reason": "not_an_object"}
    extra = set(raw.keys()) - _PROPOSAL_KEYS
    if extra:
        return {"ok": False, "reason": f"unknown_keys:{sorted(extra)[:3]}"}
    operation = str(raw.get("operation") or "")
    if operation not in ALL_OPERATIONS:
        return {"ok": False, "reason": f"unknown_operation:{operation[:32]}"}
    target_type = str(raw.get("target_type") or "none")
    if target_type not in TARGET_TYPES:
        return {"ok": False, "reason": f"bad_target_type:{target_type[:32]}"}
    coords = raw.get("coordinates")
    if target_type == "coordinates":
        if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
            return {"ok": False, "reason": "coordinates_required"}
        try:
            x, y = int(coords[0]), int(coords[1])
        except (TypeError, ValueError):
            return {"ok": False, "reason": "coordinates_not_numeric"}
        vp = viewport or {"width": 1280, "height": 720}
        if not (0 <= x <= int(vp.get("width", 0))
                and 0 <= y <= int(vp.get("height", 0))):
            return {"ok": False, "reason": "coordinate_out_of_viewport"}
    proposal = visual_proposal(
        operation=operation, target_type=target_type,
        target=str(raw.get("target") or ""),
        coordinates=coords if isinstance(coords, (list, tuple)) else None,
        arguments=raw.get("arguments") if isinstance(
            raw.get("arguments"), dict) else None,
        reason=str(raw.get("reason") or ""),
        confidence=_safe_float(raw.get("confidence"), 0.5),
        expected_result=raw.get("expected_result") if isinstance(
            raw.get("expected_result"), dict) else None,
        observed_page_version=_safe_int(raw.get("observed_page_version")),
        observed_observation_id=str(raw.get("observed_observation_id") or ""),
        consequential=bool(raw.get("consequential")),
    )
    return {"ok": True, "proposal": proposal}


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
