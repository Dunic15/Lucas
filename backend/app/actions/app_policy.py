"""Per-org connected-app catalog and the one policy decision point.

Two jobs, both of which used to be scattered:

1. **Catalog** — what THIS org can actually drive, derived from the accounts it
   really connected in Pipedream Connect plus Laura's own native adapters. It is
   built from live connection state, never from a hand-kept list, so an app
   connected a minute ago is available to OpenClaw Chat *and* to the in-meeting
   tool brief without a code change. It deliberately does NOT call Pipedream's
   pre-built action/component catalog: that surface sits behind a higher plan
   tier, and nothing here may depend on it.

2. **Policy** — one ``evaluate()`` that answers, for a single API operation:
   is it allowed, is it a read or a write, how risky is it, and does it need
   approval. Every generic request — planner read, workflow step, executor
   write — goes through it, so "reads may be automatic, writes never are" is
   enforced in one place rather than re-argued at each call site.

The approval invariant is not configurable: ``requires_approval`` is True for
every write this module ever authorises. An allow-list can only ever *remove*
capability.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..config import settings
from . import app_registry

_RISK_ORDER = ("low", "medium", "high")
# Commas and newlines only — NEVER whitespace: an operation rule carries a
# space inside it ("github:DELETE *"), so splitting on spaces would silently
# shred the rule into "github:DELETE" plus a bare "*" that denies everything.
_SPLIT = re.compile(r"[,\n]+")
_MAX_BODY_BYTES = 64 * 1024
_MAX_URL_CHARS = 4000


@dataclass(frozen=True)
class Decision:
    """The verdict for one connected-app operation."""

    allow: bool
    intent: str                 # "read" | "write"
    risk: str                   # low | medium | high
    requires_approval: bool
    reason: str = ""

    @property
    def is_read(self) -> bool:
        return self.intent == "read"


@dataclass(frozen=True)
class ValidatedRequest:
    """A model-proposed API request that survived parsing + policy."""

    app: str
    method: str
    url: str
    path: str
    body: Any
    headers: dict[str, str]
    decision: Decision


def _risk_max(*values: str) -> str:
    best = 0
    for value in values:
        try:
            best = max(best, _RISK_ORDER.index(str(value or "").strip().lower()))
        except ValueError:
            continue
    return _RISK_ORDER[best]


def _rules(raw: str) -> list[tuple[str, str]]:
    """Parse ``"stripe, hubspot:DELETE *"`` into ``[(slug, op_glob)]``."""
    out: list[tuple[str, str]] = []
    for part in _SPLIT.split(str(raw or "")):
        item = part.strip()
        if not item:
            continue
        slug, sep, op = item.partition(":")
        out.append((slug.strip().lower(), op.strip() if sep else ""))
    return out


def _denied(app: str, method: str, path: str) -> str:
    """Operator deny rules from ``CONNECTED_APP_DENY``; '' when allowed."""
    for slug, op in _rules(getattr(settings, "connected_app_deny", "")):
        if slug not in (app, "*"):
            continue
        if not op:
            return f"{app} is blocked by this workspace's connected-app policy"
        if app_registry.operation_matches((op,), method, path):
            # Name the OPERATION, not just the app: a receipt that says
            # "github is blocked" when only DELETE is blocked reads as an
            # outage rather than as the policy doing its job.
            return (
                f"{method} {path} is not permitted on {app} by this "
                "workspace's connected-app policy"
            )
    return ""


def _allowlisted(app: str) -> bool:
    """``CONNECTED_APP_ALLOW`` empty ⇒ every connected app is in scope."""
    rules = _rules(getattr(settings, "connected_app_allow", ""))
    if not rules:
        return True
    return any(slug in (app, "*") for slug, _ in rules)


def evaluate(app_slug: str, method: str, url: str) -> Decision:
    """Classify + authorise one API operation. Pure: no network, no org state.

    Callers still have to prove the org actually connected the app — that is
    connection state, not policy, and it lives in ``catalog()`` / the executor's
    account lookup.
    """
    app = str(app_slug or "").strip().lower()
    verb = str(method or "").strip().upper()
    spec = app_registry.spec_for(app)
    try:
        path = urlsplit(str(url or "")).path or "/"
    except ValueError:
        path = "/"
    read = app_registry.is_read_operation(spec, verb, path)
    intent = "read" if read else "write"
    # A write is never auto-executed. This is the invariant the whole
    # Action Center rests on, so it is computed from intent alone.
    requires_approval = not read

    if spec is None:
        return Decision(
            False, intent, "high", requires_approval,
            f"{app or 'this app'} has no registered API host, so Laura cannot "
            "build a generic request for it. It needs a deterministic adapter "
            "or a registry entry.",
        )
    if verb not in app_registry.METHODS:
        return Decision(
            False, intent, "high", requires_approval,
            "API method must be GET, HEAD, POST, PUT, PATCH or DELETE",
        )
    if not _allowlisted(app):
        return Decision(
            False, intent, "high", requires_approval,
            f"{app} is not in this workspace's connected-app allow list",
        )
    if reason := _denied(app, verb, path):
        return Decision(False, intent, "high", requires_approval, reason)
    if app_registry.operation_matches(spec.deny_ops, verb, path):
        return Decision(
            False, intent, "high", requires_approval,
            f"{verb} {path} is not permitted on {app}",
        )
    risk = (
        "low" if read
        else _risk_max(spec.write_risk, "high" if verb == "DELETE" else "medium")
    )
    return Decision(True, intent, risk, requires_approval, "")


def validate_request(
    args: dict, *, read_only: bool = False
) -> ValidatedRequest:
    """Parse, bound and authorise a model-proposed Connect Proxy request.

    Raises ``ValueError`` with a human-readable reason — the single funnel every
    generic request passes through, so URL parsing, the SSRF host check, the
    header allow-list and the policy verdict can never disagree.
    """
    values = args if isinstance(args, dict) else {}
    app = str(values.get("app") or "").strip().lower()
    method = str(values.get("method") or "").strip().upper()
    url = str(values.get("url") or "").strip()
    spec = app_registry.spec_for(app)
    if spec is None:
        raise ValueError(f"app {app!r} has no registered API host")
    if method not in app_registry.METHODS:
        raise ValueError("API method must be GET, HEAD, POST, PUT, PATCH or DELETE")
    if len(url) > _MAX_URL_CHARS:
        raise ValueError("API URL is too long")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("API URL is invalid") from exc
    host = str(parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("API URL must be a plain HTTPS URL")
    if not app_registry.host_allowed(spec, host):
        raise ValueError(f"{host!r} is not an approved API host for {app}")

    path = parsed.path or "/"
    decision = evaluate(app, method, url)
    if not decision.allow:
        raise ValueError(decision.reason or f"{method} {path} is not permitted on {app}")
    if read_only and not decision.is_read:
        raise ValueError("planner reads may not perform this API operation")

    body = values.get("body")
    if body is not None:
        try:
            encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if len(encoded.encode("utf-8")) > _MAX_BODY_BYTES:
            raise ValueError("request body is too large")
    raw_headers = values.get("headers")
    raw_headers = raw_headers if isinstance(raw_headers, dict) else {}
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        name = str(key or "").strip()
        if name.lower() not in app_registry.SAFE_HEADERS:
            raise ValueError(f"request header {name!r} is not allowed")
        headers[name] = str(value or "")[:200]
    # Vendor headers the API requires (Notion-Version and friends) come from
    # the registry row, not from a branch on the app name.
    for name, value in spec.default_headers().items():
        if not any(key.lower() == name.lower() for key in headers):
            headers[name] = value
    return ValidatedRequest(
        app=app, method=method, url=url, path=path,
        body=body, headers=headers, decision=decision,
    )


# ── per-org catalog ─────────────────────────────────────────────────────────

def _connected_pipedream_apps(org_id: str) -> set[str]:
    """Healthy Pipedream accounts for this org. Best-effort: [] on any failure."""
    try:
        from .. import pipedream_client, pipedream_executor

        # One gate, the executor's own — re-deriving it from the flag and the
        # client would let this module disagree with the plane it describes.
        if not pipedream_executor.enabled():
            return set()
        return {
            str(account.get("app") or "")
            for account in pipedream_client.list_accounts(str(org_id or ""))
            if account.get("healthy", True) and str(account.get("app") or "")
        }
    except Exception:  # noqa: BLE001 — the catalog is never load-bearing
        return set()


def _native_types(org_id: str) -> dict[str, list[str]]:
    """Connected native adapter types, grouped by the app slug that owns them."""
    out: dict[str, list[str]] = {}
    try:
        from .. import native_runtime, pipedream_executor

        for item in native_runtime.catalog(str(org_id or "")):
            if not item.get("connected"):
                continue
            action_type = str(item.get("type") or "")
            app = pipedream_executor.app_for_type(action_type) or (
                str(item.get("family") or "")
            )
            if app and action_type:
                out.setdefault(app, []).append(action_type)
    except Exception:  # noqa: BLE001
        return out
    return {app: sorted(set(types)) for app, types in out.items()}


def _mapped_types() -> dict[str, list[str]]:
    try:
        from .. import pipedream_executor

        return pipedream_executor.types_by_app()
    except Exception:  # noqa: BLE001
        return {}


def _entry(slug: str, sources: list[str], deterministic: list[str]) -> dict:
    spec = app_registry.spec_for(slug)
    # Root-path probes: `evaluate` classifies the operation, it never checks the
    # host (that is `validate_request`'s job, per real request). So a
    # pattern-host app like Jira is answered here exactly like an exact-host one.
    read = evaluate(slug, "GET", "/")
    write = evaluate(slug, "POST", "/")
    can_read = read.allow
    can_write = write.allow
    limit = ""
    if spec is None:
        limit = (
            f"{slug} is connected, but Laura has no registered API surface for "
            "it. It can only run the deterministic actions listed here; a "
            "generic API call needs an adapter or a registry entry."
        )
    elif not can_write:
        limit = write.reason or f"{slug} writes are blocked by policy"
    elif spec.note:
        limit = spec.note
    return {
        "slug": slug,
        "label": spec.label if spec else slug.replace("_", " ").title(),
        "connected": True,
        "sources": sorted(set(sources)),
        "deterministic_types": sorted(set(deterministic)),
        "api_hosts": (
            sorted(spec.hosts) + sorted(spec.host_patterns) if spec else []
        ),
        "guides": list(spec.guides) if spec else [],
        "can_read": can_read,
        "can_write": can_write,
        # Never negotiable: a side effect always stops at the approve door.
        "requires_approval": True,
        "adapter_required": spec is None,
        "risk": write.risk,
        "limit": limit,
        "note": spec.note if spec else "",
    }


def catalog(org_id: str) -> list[dict]:
    """Every app THIS org has actually connected, with what Laura can do with it.

    Never raises and never depends on Pipedream's plan-gated component catalog.
    """
    org = str(org_id or "").strip()
    mapped = _mapped_types()
    native = _native_types(org)
    connected = _connected_pipedream_apps(org)

    sources: dict[str, list[str]] = {}
    deterministic: dict[str, list[str]] = {}
    for slug in connected:
        sources.setdefault(slug, []).append("pipedream")
        deterministic.setdefault(slug, []).extend(mapped.get(slug, []))
    for slug, types in native.items():
        sources.setdefault(slug, []).append("native")
        deterministic.setdefault(slug, []).extend(types)
    return [
        _entry(slug, sources[slug], deterministic.get(slug, []))
        for slug in sorted(sources)
    ]


def connected_slugs(org_id: str) -> list[str]:
    """Just the slugs — the cheap question the tool brief and gates ask."""
    return [entry["slug"] for entry in catalog(org_id)]


def entry_for(org_id: str, app_slug: str) -> dict | None:
    slug = str(app_slug or "").strip().lower()
    return next((e for e in catalog(org_id) if e["slug"] == slug), None)


def verbs_for(app_slug: str, deterministic: list[str] | None = None) -> list[str]:
    """Short capability words for the in-meeting tool brief — plan-free.

    Derived from the deterministic types the executor really has plus, when the
    app has a registered API surface, the generic read/write capability. This is
    what lets a just-connected app be *described* to a meeting without calling
    Pipedream's paid action catalog.
    """
    out = [
        str(t).split(".", 1)[-1].replace("_", " ")
        for t in (deterministic or [])
        if str(t).strip()
    ]
    # The generic phrasing is a FALLBACK, not an addition. This list rides in
    # the per-turn meeting brief (hard-capped, on the live path), so an app that
    # already has concrete deterministic verbs keeps naming those — the generic
    # sentence only rescues an app that would otherwise have nothing to say.
    spec = app_registry.spec_for(app_slug)
    if not out and spec is not None and evaluate(spec.slug, "POST", "/").allow:
        out = ["read data", "create and update records"]
    seen: list[str] = []
    for verb in out:
        if verb and verb not in seen:
            seen.append(verb)
    return seen[:6]


def planner_limits(entries: list[dict]) -> list[str]:
    """One honest sentence per app whose capability is bounded.

    Handed to the planner so it explains the boundary — "this needs an adapter"
    — instead of inventing an execution it cannot perform.
    """
    return [
        f"{entry['slug']}: {entry['limit']}"
        for entry in entries
        if entry.get("limit")
    ][:12]
