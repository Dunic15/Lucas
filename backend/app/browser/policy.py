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

import hashlib
import re
from typing import Any
from urllib.parse import urlparse

# Command verbs the operator dispatches. B1 adds wait/inspect/go_back
# additively (all read-only); the coordinator-only control ops
# (finish/request_human_help) are NEVER sent to the operator.
READ_ONLY_VERBS = ("observe", "navigate", "scroll", "click", "type",
                   "wait", "inspect", "go_back")

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


def _clamp_bbox(bbox: Any, viewport: dict) -> list | None:
    """Bound an element bounding box to the viewport, as [x0,y0,x1,y1] ints —
    the geometry coordinate hit-testing resolves against. Reject garbage."""
    if not (isinstance(bbox, (list, tuple)) and len(bbox) >= 4):
        return None
    try:
        x0, y0, x1, y1 = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
    except (TypeError, ValueError):
        return None
    w = int(viewport.get("width", 1280))
    h = int(viewport.get("height", 720))
    x0, x1 = sorted((max(0, min(x0, w)), max(0, min(x1, w))))
    y0, y1 = sorted((max(0, min(y0, h)), max(0, min(y1, h))))
    return [x0, y0, x1, y1]


def sanitize_observation(raw: Any, *, max_dom: int = 2000,
                         max_elements: int = 40) -> dict:
    """Turn a RawObservation into the bounded, secret-free, BYTE-FREE
    observation contract (spec §8 + B1). Never emits raw credential/secret
    values or screenshot bytes: the screenshot leaves only as a ref + a sha256
    digest. Marks sensitive fields by shape and carries element geometry
    (bbox) for coordinate hit-testing."""
    viewport = dict(getattr(raw, "viewport", None) or {"width": 1280,
                                                       "height": 720})
    url = redact(getattr(raw, "url", ""))
    title = redact(getattr(raw, "title", ""))
    dom = redact(getattr(raw, "dom_summary", ""))[:max_dom]
    a11y = redact(getattr(raw, "accessibility_summary", ""))[:1000]
    elements = []
    for el in (getattr(raw, "elements", []) or [])[:max_elements]:
        kind = str(el.get("kind") or "")
        sensitive = kind in _SENSITIVE_ELEMENT_KINDS
        # DEFENSE IN DEPTH (adversarial finding): a credential/mfa/secret field
        # value must NEVER become the element name, even if a provider sourced
        # it — the shape-based redact() would miss a plain password/OTP. Blank
        # the name for sensitive kinds unconditionally.
        name = "" if sensitive else redact(str(el.get("name") or ""))
        href = el.get("href")
        elements.append({
            "id": str(el.get("id") or ""),
            "role": str(el.get("role") or ""),
            "name": name,
            "kind": kind,
            "sensitive": sensitive,
            "bbox": _clamp_bbox(el.get("bbox"), viewport),
            # href (redacted, bounded) so the navigation allowlist can gate a
            # link CLICK, not only an explicit navigate.
            "href": redact(str(href))[:300] if isinstance(href, str) and href
            else "",
        })
    # The screenshot BYTES are digested here and then dropped — they never
    # enter the returned/persisted observation. Only ref + digest + length go on.
    shot_bytes = getattr(raw, "screenshot_bytes", b"") or b""
    digest = hashlib.sha256(shot_bytes).hexdigest() if shot_bytes else ""
    return {
        "url": url,
        "title": title,
        "viewport": viewport,
        "dom_summary": dom,
        "a11y_summary": a11y,
        "elements": elements,
        # Redacted too: a fake ref embeds the URL, which may carry a token.
        "screenshot_ref": redact(str(getattr(raw, "screenshot_ref", "") or "")),
        "screenshot_digest": digest,
        "screenshot_bytes_len": len(shot_bytes),
        "truncated": bool(getattr(raw, "truncated", False))
        or len(getattr(raw, "elements", []) or []) > max_elements,
    }


# ── B1 navigation allowlist (server-authoritative, page can only NARROW) ─────

def allowed_domain_set(raw: str) -> set[str]:
    return {d.strip().lower() for d in str(raw or "").split(",") if d.strip()}


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def check_navigation_target(url: str, allowed_domains: set[str], *,
                            observation_links: set[str] | None = None) -> dict:
    """SEPARATE from classify() (which stays B0-byte-identical). A navigate
    target is permitted ONLY when: scheme is http(s), AND its host is in the
    server allowlist, AND (it is a link present in the current observation OR
    the allowlist is the sole gate when no link set is provided). Page content
    can NEVER widen the allowlist — an on-page link is an ADDITIONAL constraint,
    never an alternative. Returns {ok, reason}."""
    url = str(url or "")
    scheme = (urlparse(url).scheme or "").lower() if url else ""
    if scheme not in ("http", "https"):
        return {"ok": False, "reason": "scheme_not_allowed"}
    host = _host(url)
    if not host:
        return {"ok": False, "reason": "no_host"}
    # host must match an allowed domain (exact or a subdomain of it).
    allowed = any(host == d or host.endswith("." + d) for d in allowed_domains)
    if not allowed:
        return {"ok": False, "reason": "domain_blocked"}
    if observation_links is not None and url not in observation_links:
        # When we constrain to on-page links, the target must actually be one.
        return {"ok": False, "reason": "not_a_current_link"}
    return {"ok": True, "reason": ""}


def resolve_coordinate(observation: dict, x: int, y: int) -> str | None:
    """Hit-test a viewport coordinate to an element id via its bbox — so a
    coordinate action runs the EXACT SAME kind-classification as an element
    action (never a coordinate-only bypass). Returns the element id or None
    (None ⇒ blocked, mirroring 'element not in live DOM')."""
    for el in observation.get("elements") or []:
        bbox = el.get("bbox")
        if bbox and len(bbox) >= 4 and bbox[0] <= x <= bbox[2] \
                and bbox[1] <= y <= bbox[3]:
            return str(el.get("id") or "") or None
    return None


def safe_param_projection(verb: str, element: dict, text: str = "") -> dict:
    """What a guarded-step approver actually sees (spec §8) — the safe param
    projection that lands in the canonical Action's typed_json and the card.
    Never full page content, never secret values."""
    return {
        "action": str(element.get("kind") or verb),
        "target": redact(str(element.get("name") or element.get("id") or "")),
        "text": redact(text)[:200] if text else "",
    }
