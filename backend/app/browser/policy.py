"""Deterministic browser policy engine (B0) — the authority, not the model.

Between any planner suggestion and the provider, deterministic code decides
the action CLASS and sanitizes what leaves the boundary. Page content is
untrusted input: an on-page instruction can never widen a class or skip a
check (prompt-injection posture).

Three classes (spec §8):
- ``auto``    — navigate/scroll/read/find/screenshot + NON-submit typing into
                NON-credential fields. Executes after checks (read-only in B0).
- ``guarded`` — form submit; send; purchase; publish; delete; external
                comms; file upload; account creation. Becomes a canonical
                Action requiring approval — NEVER executed inline.
- ``blocked`` — credential/secret/token extraction; anything touching MFA
                (OTP/recovery); downloads/executables; security-setting
                changes. Refused and surfaced (distilled).

B0 posture: read-only demonstrations. ``auto`` executes; ``guarded`` routes
to the approval plane (proving the path with the fake provider), and can be
hard-rejected when ``BROWSER_ALLOW_WRITES`` is off (the default) so B0 ships
strictly read-only while still exercising the routing contract in tests.
"""
from __future__ import annotations

import re
from typing import Any

# Command verbs the operator accepts.
READ_ONLY_VERBS = ("observe", "navigate", "scroll", "click", "type")

# Element kinds (from the DOM summary) that make a WRITE.
_GUARDED_KINDS = {
    "purchase", "send", "submit", "publish", "delete", "account_creation",
    "upload", "external_comm",
}
_BLOCKED_KINDS = {"credential", "secret", "mfa", "download", "security_setting"}

_CREDENTIAL_NAME = re.compile(
    r"password|passcode|one[- ]?time|otp|2fa|mfa|recovery|secret|token|api[- ]?key",
    re.IGNORECASE,
)
# Sensitive value shapes redacted out of observations/receipts.
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}|"
    r"[A-Za-z0-9]{16,}\.[A-Za-z0-9]{16,}\.[A-Za-z0-9._-]{8,}|"
    r"AKIA[0-9A-Z]{12,}|ghp_[A-Za-z0-9]{20,}|whsec_[A-Za-z0-9]{16,})",
)


def _element(observation: dict, element_id: str) -> dict | None:
    for el in observation.get("elements") or []:
        if str(el.get("id")) == str(element_id):
            return el
    return None


def classify(verb: str, observation: dict, *, element_id: str = "",
             text: str = "", allowlist: list[str] | None = None) -> dict:
    """Return {class, reason, element_kind}. class ∈ auto|guarded|blocked.

    ``observation`` is the CURRENT page (the operator observes before acting,
    so classification runs against live DOM — a coordinates-only click with no
    resolvable element is refused)."""
    verb = str(verb or "").lower()
    if verb in ("observe", "scroll"):
        return {"class": "auto", "reason": "read-only", "element_kind": ""}
    if verb == "navigate":
        # Read-only browsing on any page is allowed; an authenticated-profile
        # allowlist would gate here (B5). B0 has no profiles → always auto.
        return {"class": "auto", "reason": "navigate", "element_kind": ""}
    if verb not in ("click", "type"):
        return {"class": "blocked", "reason": f"unknown verb {verb!r}",
                "element_kind": ""}

    el = _element(observation, element_id)
    if el is None:
        # No resolvable element in the live DOM — refuse (no coord-only acts).
        return {"class": "blocked", "reason": "element not in live DOM",
                "element_kind": ""}
    kind = str(el.get("kind") or "")

    if kind in _BLOCKED_KINDS or _CREDENTIAL_NAME.search(str(el.get("name") or "")):
        return {"class": "blocked",
                "reason": f"forbidden element kind {kind or 'credential'}",
                "element_kind": kind}
    if verb == "type":
        # Typing into a plain text field is auto; into anything sensitive is
        # already blocked above.
        return {"class": "auto", "reason": "text entry", "element_kind": kind}
    # verb == click
    if kind in _GUARDED_KINDS:
        return {"class": "guarded",
                "reason": f"write action: {kind}", "element_kind": kind}
    return {"class": "auto", "reason": "safe click", "element_kind": kind}


def redact(text: str) -> str:
    """Strip secret-shaped substrings from any text that will be observed,
    logged, or put in a receipt."""
    return _SECRET_VALUE.sub("[REDACTED]", str(text or ""))


_SENSITIVE_ELEMENT_KINDS = _BLOCKED_KINDS


def sanitize_observation(raw: Any, *, max_dom: int = 2000,
                         max_elements: int = 40) -> dict:
    """Turn a RawObservation into the bounded, secret-free observation
    contract (spec §8). Never emits raw credential/secret values, and marks
    sensitive fields by shape only."""
    url = redact(getattr(raw, "url", ""))
    title = redact(getattr(raw, "title", ""))
    dom = redact(getattr(raw, "dom_summary", ""))[:max_dom]
    elements = []
    for el in (getattr(raw, "elements", []) or [])[:max_elements]:
        kind = str(el.get("kind") or "")
        elements.append({
            "id": str(el.get("id") or ""),
            "role": str(el.get("role") or ""),
            "name": redact(str(el.get("name") or "")),
            "kind": kind,
            "sensitive": kind in _SENSITIVE_ELEMENT_KINDS,
        })
    return {
        "url": url,
        "title": title,
        "dom_summary": dom,
        "elements": elements,
        "screenshot_ref": str(getattr(raw, "screenshot_ref", "") or ""),
        "truncated": bool(getattr(raw, "truncated", False))
        or len(getattr(raw, "elements", []) or []) > max_elements,
    }


def safe_param_projection(verb: str, element: dict, text: str = "") -> dict:
    """What a guarded-step approver actually sees (spec §8) — the safe param
    projection that lands in the canonical Action's typed_json and the card.
    Never full page content, never secret values."""
    return {
        "action": str(element.get("kind") or verb),
        "target": redact(str(element.get("name") or element.get("id") or "")),
        "text": redact(text)[:200] if text else "",
    }
