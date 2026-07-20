"""Approved-action executor backed by Laura's native Action Runtime.

The canonical approval doors and ledger execution claim remain the only license
to perform a consequential write.  Once claimed, this module asks Laura's own
adapter registry to select and call the connected tool.  It never brokers an
action to an external executor.

Slack and the dashboard are control surfaces only: they may approve, reject,
and display status, but the vendor call and receipt are owned here inside Laura.
"""
from __future__ import annotations

# Re-export the concrete clients for compatibility with the existing executor
# tests and any callers that monkeypatch these seams.  The runtime owns routing;
# these names are not a second execution path.
from . import asana_client, google_client, ledger, native_runtime
from .config import settings

CALENDAR_CREATE = "calendar.create_event"
EMAIL_SEND = "email.send"
ASANA_CREATE = "asana.create_task"
ASANA_UPDATE = "asana.update_task"
ASANA_COMMENT = "asana.add_comment"
SLACK_POST = "slack.post_message"
ASANA_ACTION_TYPES = frozenset({ASANA_CREATE, ASANA_UPDATE, ASANA_COMMENT})
NATIVE_ACTION_TYPES = native_runtime.action_types()


def capability_family(action_type: str | None) -> str:
    """Per-avatar capability family governing this action type."""
    return native_runtime.family_for(action_type) or "google"


def from_typed(typed: dict | None) -> dict | None:
    """Return a normalized native action only when Laura owns its adapter."""
    return native_runtime.from_typed(typed)


def enabled() -> bool:
    """Whether the Laura-native execution plane is active."""
    return bool(settings.native_executor)


def handles(action: dict | None) -> bool:
    """True when this approved action can execute inside Laura."""
    return enabled() and native_runtime.supports((action or {}).get("type"))


def _normalized(action: dict | None) -> dict:
    """Accept the new ``{type,args}`` shape and legacy nested caller shapes."""
    action = action if isinstance(action, dict) else {}
    atype = str(action.get("type") or "").strip()
    args = action.get("args") if isinstance(action.get("args"), dict) else None
    if args is None:
        if atype == CALENDAR_CREATE and isinstance(action.get("event"), dict):
            args = action["event"]
        elif atype == EMAIL_SEND and isinstance(action.get("message"), dict):
            args = action["message"]
        elif atype in ASANA_ACTION_TYPES and isinstance(action.get("task"), dict):
            args = action["task"]
        elif atype == SLACK_POST and isinstance(action.get("message"), dict):
            args = action["message"]
        else:
            args = {
                key: value
                for key, value in action.items()
                if key not in {"type", "event", "message", "task"}
            }
    return {"type": atype, "args": dict(args)}


def execute_approved(org_id: str, action_id: str, action: dict) -> dict:
    """Execute one approved action and settle its canonical ledger receipt.

    This function never raises.  Missing connections, unsupported action types,
    and vendor failures all become truthful ``failed`` receipts.
    """
    if not enabled():
        return {"ok": False, "skipped": "native_executor off"}
    normalized = _normalized(action)
    atype = normalized.get("type")
    if not native_runtime.supports(atype):
        return {"ok": False, "skipped": f"unhandled action type {atype!r}"}
    org = str(org_id or "").strip()
    if not org:
        return {"ok": False, "error": "missing org"}

    result = native_runtime.execute(org, normalized)
    what = str(result.get("kind") or atype or "action")
    receipt = str(result.get("ref") or "")

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

        # Best-effort status projection to the Slack surface.  This does not
        # execute or route the action; Laura already performed the vendor call.
        try:
            from .cedric import callback as slack_surface

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
