"""Approved-action executor backed by Laura's native Action Runtime.

The canonical approval doors and ledger execution claim remain the only license
to perform a consequential write. Once claimed, this module asks Laura's own
adapter registry to select and call the connected tool. Slack posts are the one brokered exception: they execute through the org-scoped
Cedric connection. All other supported writes execute inside Laura.

Slack and the dashboard are control surfaces only: they may approve, reject,
and display status, but the vendor call and receipt are owned here inside Laura.
"""
from __future__ import annotations

import re

# Re-export concrete clients for compatibility with existing tests and callers
# that monkeypatch these seams. The runtime owns routing; these names are not a
# second execution path.
from . import ledger
from .. import asana_client, google_client, native_runtime
from ..config import settings

CALENDAR_CREATE = "calendar.create_event"
EMAIL_SEND = "email.send"
ASANA_CREATE = "asana.create_task"
ASANA_UPDATE = "asana.update_task"
ASANA_COMMENT = "asana.add_comment"
SLACK_POST = "slack.post_message"
ASANA_ACTION_TYPES = frozenset({ASANA_CREATE, ASANA_UPDATE, ASANA_COMMENT})
# Gmail send and Calendar create exist on both planes. They deliberately route
# through this executor first: native org OAuth wins; Pipedream Connect is the
# fallback when native authentication is unavailable before any vendor write.
_GOOGLE_FALLBACK_ACTION_TYPES = frozenset({CALENDAR_CREATE, EMAIL_SEND})
NATIVE_ACTION_TYPES = native_runtime.action_types()


def capability_family(action_type: str | None) -> str:
    """Per-avatar capability family governing this action type. A generic
    Pipedream action (``pd.<app>.run``) is governed by its APP's own toggle —
    never lumped under google."""
    from .. import pipedream_executor  # lazy: keep module load order decoupled

    app = pipedream_executor.generic_app(action_type)
    if app:
        return app
    if str(action_type or "").strip() == SLACK_POST:
        return "slack"
    return native_runtime.family_for(action_type) or "google"


def from_typed(typed: dict | None) -> dict | None:
    """Bridge ``{type,args}`` to the established executor action boundary.

    The native runtime uses one normalized ``args`` object internally, but the
    approval/finalize callers and tests already rely on the nested legacy shape.
    Preserving it here makes the runtime an implementation detail rather than a
    breaking API change.
    """
    action_type = str((typed or {}).get("type") or "").strip()
    if action_type == SLACK_POST:
        args = typed.get("args") if isinstance(typed.get("args"), dict) else {}
        return {"type": action_type, "message": dict(args)}

    normalized = native_runtime.from_typed(typed)
    if normalized is None:
        # Generic Pipedream actions (pd.<app>.run) aren't native, but they ARE
        # executable when the Pipedream plane is on — pass them through so the
        # approve doors and route_for_typed see one consistent shape. With the
        # plane OFF they stay None → cedric, byte-identical to before.
        from .. import pipedream_executor

        t = str((typed or {}).get("type") or "").strip()
        if pipedream_executor.generic_app(t) and pipedream_executor.enabled():
            args = typed.get("args") if isinstance(typed.get("args"), dict) else {}
            return {"type": t, "args": dict(args)}
        return None
    action_type = normalized["type"]
    args = normalized["args"]
    if action_type == CALENDAR_CREATE:
        return {"type": action_type, "event": args}
    if action_type == EMAIL_SEND:
        return {"type": action_type, "message": args}
    if action_type in ASANA_ACTION_TYPES:
        return {"type": action_type, "task": args}
    if action_type == SLACK_POST:
        return {"type": action_type, "message": args}
    return None


def enabled() -> bool:
    """Whether the Laura-native execution plane is active."""
    return bool(settings.native_executor)


def capability_blocked(caps: dict | None, action_type: str | None) -> bool:
    """ONE policy for every approve door: does this avatar's toggle set block
    this action? Native families (google/slack/asana) keep default-ON semantics
    — blocked only on an explicit OFF. Generic Pipedream apps (pd.<app>.run)
    are explicit OPT-IN — blocked unless the owner turned that app ON for this
    avatar (emission is gated the same way; this is the defense in depth)."""
    from .. import pipedream_executor  # lazy: keep module load order decoupled

    caps = caps or {}
    family = capability_family(action_type)
    if pipedream_executor.generic_app(action_type):
        return caps.get(family) is not True
    if family == "google":
        # The dashboard split the old combined "Google" switch into per-app
        # toggles (gmail / google_calendar / google_drive). Honor the CONCRETE
        # per-app toggle for this action type and IGNORE the legacy combined
        # 'google' row — otherwise a stale google=0 vetoes Gmail/Calendar even
        # when gmail=1 / google_calendar=1 are individually ON (live bug
        # 2026-07-21: every approved Gmail/Calendar action silently blocked).
        app = pipedream_executor.app_for_type(action_type)  # email.send→gmail, calendar.*→google_calendar
        if app:
            return caps.get(app) is False
    return caps.get(family) is False


def _cedric_linked(org_id: str) -> bool:
    """Whether this org actually HAS the Slack agent (a connected cedric-brain
    row). Owner rule 2026-07-22: "Cedric lives inside Slack" — an org that
    never linked it must never see a 'Runs through Cedric' card, a doomed
    dispatch, or the credentials-unavailable noise. The demo org keeps the
    legacy service-scope behavior (its Cedric wiring is global env)."""
    org = str(org_id or "").strip()
    if not org or org == settings.demo_org_id:
        return True
    try:
        from .. import store  # lazy: keep module load order decoupled

        return any(
            r.get("provider") == "cedric-brain" and r.get("status") == "connected"
            for r in store.connections_for_org(org)
        )
    except Exception:  # noqa: BLE001 — no store, no claim
        return False


def _brokered_route(org_id: str) -> str:
    """The route for work Laura can't execute herself: the Slack agent when
    the org has one, otherwise 'manual' (a track-only card — recorded for the
    humans, nothing pretends to run it)."""
    return "cedric" if _cedric_linked(org_id) else "manual"


# An UNTYPED capture goes to Cedric only when the humans literally asked for
# Slack (owner rule 2026-07-22, part two: even a linked org must not use the
# Slack agent as the catch-all for every unexecutable ask — "manda su Slack"
# routes to him, "sort out the vendor situation" stays a tracked card).
_SLACK_ASK = re.compile(r"(?:\bslack\b|(?:^|\s)#[a-z0-9][a-z0-9_-]{1,40}\b)", re.IGNORECASE)


def _untyped_route(org_id: str, item_text: str) -> str:
    if _cedric_linked(org_id) and _SLACK_ASK.search(item_text or ""):
        return "cedric"
    # Demo/no-org service scope keeps the legacy catch-all (Cedric's own
    # demo flows depend on it).
    org = str(org_id or "").strip()
    if not org or org == settings.demo_org_id:
        return "cedric"
    return "manual"


def resolve_effective_route(
    typed: dict | None,
    org_id: str = "",
    item_text: str = "",
    *,
    acting_avatar: str = "",
    explicit_route: str = "",
) -> dict:
    """Resolve the route from current organization state.

    Stored legacy routes are deliberately not inputs: connections and the
    Pipedream plane can change between capture and approval. The sole explicit
    override is browser, whose guarded step was intentionally chosen by a
    human. The returned capability flag lets every approval surface apply the
    same avatar policy without changing the truthful provider label.
    """
    from .. import pipedream_executor  # lazy: keep module load order decoupled

    action_type = str((typed or {}).get("type") or "").strip()
    explicit = str(explicit_route or "").strip().lower()
    if explicit == "browser":
        route = "browser"
    elif action_type == SLACK_POST:
        route = _brokered_route(org_id)
    elif pipedream_executor.handles({"type": action_type}):
        app = pipedream_executor.app_for_type(action_type)
        if pipedream_executor.app_connected(org_id, app):
            route = "pipedream"
        elif native_runtime.supports(action_type):
            route = "native" if enabled() else _brokered_route(org_id)
        else:
            # Long-tail actions have no native adapter. Keep the truthful
            # Pipedream route so approval reports a missing connection instead
            # of silently falling into a different executor.
            route = "pipedream"
    elif from_typed(typed) is None:
        route = _untyped_route(org_id, item_text)
    else:
        route = "native" if enabled() else _brokered_route(org_id)

    blocked = False
    if action_type and acting_avatar:
        try:
            from .. import avatar_resolver, store

            caps = store.get_avatar_capabilities(acting_avatar, org_id)
            blocked = capability_blocked(caps, action_type) or not (
                avatar_resolver.family_allowed(
                    org_id, acting_avatar, capability_family(action_type)
                )
            )
        except Exception:  # noqa: BLE001 — approval re-checks fail safely
            blocked = False
    return {"route": route, "capability_blocked": blocked}


def route_for_typed(typed: dict | None, org_id: str = "", item_text: str = "") -> str:
    """Compatibility wrapper around the one current-state route resolver."""
    return str(resolve_effective_route(
        typed, org_id, item_text=item_text
    )["route"])
def handles(action: dict | None) -> bool:
    """True when this approved action can execute inside Laura."""
    return enabled() and native_runtime.supports((action or {}).get("type"))


def _normalized(action: dict | None) -> dict:
    """Accept the established nested shape and the runtime ``{type,args}`` shape."""
    action = action if isinstance(action, dict) else {}
    action_type = str(action.get("type") or "").strip()
    args = action.get("args") if isinstance(action.get("args"), dict) else None
    if args is None:
        if action_type == CALENDAR_CREATE and isinstance(action.get("event"), dict):
            args = action["event"]
        elif action_type == EMAIL_SEND and isinstance(action.get("message"), dict):
            args = action["message"]
        elif action_type in ASANA_ACTION_TYPES and isinstance(action.get("task"), dict):
            args = action["task"]
        elif action_type == SLACK_POST and isinstance(action.get("message"), dict):
            args = action["message"]
        else:
            args = {
                key: value
                for key, value in action.items()
                if key not in {"type", "event", "message", "task"}
            }
    return {"type": action_type, "args": dict(args)}


def _native_google_auth_unavailable(result: dict) -> bool:
    """True only when the native attempt failed before a Google write.

    Fallback is intentionally narrow: vendor/API write errors are never retried
    on another plane because the first write may have landed despite the error.
    """

    error = str((result or {}).get("error") or "").lower()
    return any(
        marker in error
        for marker in (
            "google is not connected",
            "oauth client is not configured",
            "token request failed",
            "token refresh rejected",
            "no access token returned",
        )
    )


def _pipedream_google_connected(org_id: str, action_type: str) -> bool:
    """Best-effort availability probe for the matching Pipedream Google app."""

    try:
        from .. import pipedream_executor

        return pipedream_executor.app_connected(
            org_id, pipedream_executor.app_for_type(action_type)
        )
    except Exception:  # noqa: BLE001 — native remains the truthful fallback
        return False


def _execute_pipedream_fallback(
    org_id: str, action_id: str, normalized: dict
) -> dict:
    """Execute on Pipedream; its own executor writes the canonical receipt."""

    from .. import pipedream_executor

    return pipedream_executor.execute_approved(
        org_id, action_id, normalized
    )


def execute_approved(org_id: str, action_id: str, action: dict) -> dict:
    """Execute one approved action and settle its canonical ledger receipt.

    This function never raises. Missing connections, unsupported action types,
    and vendor failures all become truthful ``failed`` receipts.
    """
    if not enabled():
        return {"ok": False, "skipped": "native_executor off"}
    normalized = _normalized(action)
    action_type = normalized.get("type")
    if not native_runtime.supports(action_type):
        return {"ok": False, "skipped": f"unhandled action type {action_type!r}"}
    org = str(org_id or "").strip()
    if not org:
        return {"ok": False, "error": "missing org"}

    # Native first for the two Google writes. If the org has no native OAuth,
    # avoid a doomed call and use its matching Connect account. If neither plan
    # exists, keep the existing native reconnect hint. A native auth failure may
    # also fall back, but a vendor/write error never does (no duplicate writes).
    if action_type in _GOOGLE_FALLBACK_ACTION_TYPES:
        try:
            from .. import store

            native_connected = bool(
                store.get_org_oauth(org, provider="google")
            )
        except Exception:  # noqa: BLE001 — concrete client remains authoritative
            native_connected = True
        if (
            not native_connected
            and _pipedream_google_connected(org, action_type)
        ):
            return _execute_pipedream_fallback(
                org, action_id, normalized
            )

    result = native_runtime.execute(org, normalized)
    if (
        action_type in _GOOGLE_FALLBACK_ACTION_TYPES
        and not result.get("ok")
        and _native_google_auth_unavailable(result)
        and _pipedream_google_connected(org, action_type)
    ):
        return _execute_pipedream_fallback(
            org, action_id, normalized
        )

    what = str(result.get("kind") or action_type or "action")
    receipt = str(result.get("ref") or "")
    # Phase-1 read-back on the NATIVE plane too (the Pipedream executor
    # already does this): re-read the object we just wrote so the receipt is
    # evidence, not presumption. Best-effort — never fails a succeeded action.
    if result.get("ok"):
        verified = False
        if action_type == "calendar.create_event" and result.get("event_id"):
            verified = google_client.verify_calendar_event(
                org, str(result["event_id"]))
        elif action_type == "email.send" and result.get("message_id"):
            verified = google_client.verify_gmail_message(
                org, str(result["message_id"]))
        if verified:
            what += " · verified"

    aid = str(action_id or "").strip()
    detail = ""
    if aid:
        try:
            if result.get("ok"):
                detail = " · ".join(
                    part for part in ("Laura native", what, receipt) if part
                )[:300]
                ledger.set_action_status(
                    aid,
                    "done",
                    detail,
                    org_id=org,
                    receipt={
                        "kind": what,
                        "ref": receipt,
                        "route": "native",
                        "runtime": "laura",
                    },
                )
            else:
                detail = (
                    f"Laura native · {result.get('error') or result.get('skipped') or 'failed'}"
                )[:300]
                ledger.set_action_status(aid, "failed", detail, org_id=org)
        except Exception as exc:  # noqa: BLE001 - execution result still returns
            print(
                f"[executor] status write skipped ({type(exc).__name__})",
                flush=True,
            )

        # Best-effort status projection to the Slack surface. This does not
        # execute or route the action; Laura already performed the vendor call.
        try:
            from ..cedric import callback as slack_surface

            slack_surface.send_action_event(
                org,
                "action.status",
                {
                    "action_id": aid,
                    "status": "done" if result.get("ok") else "failed",
                    "detail": detail[:300],
                    "receipt_url": receipt if result.get("ok") else "",
                },
            )
        except Exception:  # noqa: BLE001 - a UI mirror never breaks execution
            pass
    return result


def auto_execute_asana(org_id: str, actions: list) -> int:
    """Optional finalize-time Asana push, using the same Laura runtime."""
    if not (enabled() and settings.asana_auto_execute):
        return 0
    attempted = 0
    for item in actions or []:
        if not isinstance(item, dict):
            continue
        if item.get("source") == "inferred":
            # Petra's PROPOSED plan steps (goal decomposition) are suggestions,
            # never commitments: they reach a tool only through a human Approve
            # in the Action Centre — auto-push is for explicit asks only.
            continue
        action = from_typed(item.get("typed"))
        if action is None or action.get("type") not in ASANA_ACTION_TYPES:
            continue
        aid = str(item.get("action_id") or "")
        if not aid:
            continue
        try:
            execute_approved(org_id, aid, action)
            attempted += 1
        except Exception:  # noqa: BLE001 - finalize must never be blocked
            continue
    return attempted

