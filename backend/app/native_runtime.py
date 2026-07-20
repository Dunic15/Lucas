"""Laura-native Action Runtime.

This is the internal execution plane for approved actions.  It is deliberately
not Cedric and it never delegates execution to an external orchestrator.  Slack
and the dashboard are approval/control surfaces; this module owns adapter
selection and the real vendor call inside Laura.

Every adapter follows the same contract:

* receives an org id plus distilled, typed arguments (never transcript text);
* reports whether its connection is available for that org;
* executes exactly one approved write when called behind the canonical ledger
  execution claim; and
* returns a normalized result that the executor can persist as a receipt.

Adding a tool is one registration, not another approval system.  The built-in
adapters cover the connections Laura owns today: Google Calendar, Gmail, Asana,
and Slack webhook delivery.  Browser execution remains on its separately
hardened route because it has additional ownership and visual-verification
checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import actions as workflow_actions
from . import asana_client, google_client, store
from .config import settings

ExecuteFn = Callable[[str, dict], dict]
ConnectedFn = Callable[[str], bool]


@dataclass(frozen=True)
class Adapter:
    action_type: str
    family: str
    label: str
    execute: ExecuteFn
    connected: ConnectedFn


_ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    """Register one native adapter.

    Registration is intentionally deterministic: duplicate action types are a
    programming error rather than last-import-wins behavior.
    """
    key = str(adapter.action_type or "").strip()
    if not key:
        raise ValueError("adapter action_type is required")
    if key in _ADAPTERS:
        raise ValueError(f"native adapter already registered: {key}")
    _ADAPTERS[key] = adapter


def action_types() -> frozenset[str]:
    return frozenset(_ADAPTERS)


def supports(action_type: str | None) -> bool:
    return str(action_type or "").strip() in _ADAPTERS


def family_for(action_type: str | None) -> str:
    adapter = _ADAPTERS.get(str(action_type or "").strip())
    return adapter.family if adapter else ""


def from_typed(typed: dict | None) -> dict | None:
    """Normalize ``{type, args}`` only when Laura has a native adapter."""
    if not isinstance(typed, dict):
        return None
    action_type = str(typed.get("type") or "").strip()
    if action_type not in _ADAPTERS:
        return None
    args = typed.get("args") if isinstance(typed.get("args"), dict) else {}
    return {"type": action_type, "args": dict(args)}


def catalog(org_id: str) -> list[dict]:
    """Connection-aware catalog used by the meeting tool brief and dashboard."""
    org = str(org_id or "").strip()
    out: list[dict] = []
    for adapter in _ADAPTERS.values():
        try:
            connected = bool(adapter.connected(org))
        except Exception:  # noqa: BLE001 - catalog assembly must never block join
            connected = False
        out.append(
            {
                "type": adapter.action_type,
                "family": adapter.family,
                "name": adapter.label,
                "connected": connected,
                "write": True,
                "approval": "approve",
                "kind": "native",
            }
        )
    return out


def execute(org_id: str, action: dict) -> dict:
    """Execute one approved action through its registered native adapter.

    The canonical approval door and ledger claim live outside this module.  This
    function never raises; callers always receive a truthful normalized result.
    """
    org = str(org_id or "").strip()
    action_type = str((action or {}).get("type") or "").strip()
    adapter = _ADAPTERS.get(action_type)
    if not org:
        return {"ok": False, "error": "missing org", "kind": action_type}
    if adapter is None:
        return {
            "ok": False,
            "error": f"Laura has no native adapter for {action_type!r}",
            "kind": action_type,
        }
    try:
        if not adapter.connected(org):
            return {
                "ok": False,
                "error": f"{adapter.label} is not connected",
                "kind": adapter.label,
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"connection check failed ({type(exc).__name__})",
            "kind": adapter.label,
        }

    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    try:
        result = adapter.execute(org, dict(args))
    except Exception as exc:  # noqa: BLE001 - vendor errors become failed receipts
        result = {"ok": False, "error": f"adapter error ({type(exc).__name__})"}
    if not isinstance(result, dict):
        result = {"ok": False, "error": "adapter returned an invalid result"}
    normalized = dict(result)
    normalized["ok"] = bool(normalized.get("ok"))
    normalized.setdefault("kind", adapter.label)
    normalized.setdefault("ref", "")
    if not normalized["ok"]:
        normalized.setdefault("error", "execution failed")
    return normalized


# ---- Built-in Laura adapters -------------------------------------------------


def _google_connected(org_id: str) -> bool:
    return bool(store.get_org_oauth(org_id, provider="google"))


def _asana_connected(org_id: str) -> bool:
    return bool(asana_client.connected(org_id))


def _slack_connected(_org_id: str) -> bool:
    # The current native Slack write uses Laura's configured incoming webhook.
    # A future per-org Slack OAuth adapter can replace this registration without
    # changing the approval or execution lifecycle.
    return bool(settings.slack_webhook_url.strip())


def _calendar_create(org_id: str, args: dict) -> dict:
    result = google_client.create_calendar_event(org_id, args)
    return {
        **result,
        "kind": "calendar event",
        "ref": result.get("event_url") or result.get("event_id") or "",
    }


def _email_send(org_id: str, args: dict) -> dict:
    result = google_client.send_gmail(org_id, args)
    return {
        **result,
        "kind": "email",
        "ref": result.get("message_id") or "",
    }


def _asana_create(org_id: str, args: dict) -> dict:
    result = asana_client.create_task(org_id, args)
    return {
        **result,
        "kind": "asana task",
        "ref": result.get("task_url") or result.get("task_gid") or "",
    }


def _asana_update(org_id: str, args: dict) -> dict:
    result = asana_client.update_task(org_id, args)
    return {
        **result,
        "kind": "asana task update",
        "ref": result.get("task_url") or result.get("task_gid") or "",
    }


def _asana_comment(org_id: str, args: dict) -> dict:
    result = asana_client.add_comment(org_id, args)
    return {
        **result,
        "kind": "asana comment",
        "ref": result.get("task_url") or result.get("task_gid") or "",
    }


def _slack_post(_org_id: str, args: dict) -> dict:
    text = str(args.get("text") or args.get("message") or "").strip()
    if not text:
        return {"ok": False, "error": "Slack message text is required"}
    raw = workflow_actions.post_to_slack(text)
    ok = bool(raw.get("sent"))
    return {
        "ok": ok,
        "kind": "slack message",
        "ref": "slack:webhook" if ok else "",
        "error": "" if ok else str(raw.get("reason") or raw.get("error") or "Slack post failed"),
        "status_code": raw.get("status_code"),
    }


register(Adapter("calendar.create_event", "google", "Google Calendar", _calendar_create, _google_connected))
register(Adapter("email.send", "google", "Gmail", _email_send, _google_connected))
register(Adapter("asana.create_task", "asana", "Asana", _asana_create, _asana_connected))
register(Adapter("asana.update_task", "asana", "Asana", _asana_update, _asana_connected))
register(Adapter("asana.add_comment", "asana", "Asana", _asana_comment, _asana_connected))
register(Adapter("slack.post_message", "slack", "Slack", _slack_post, _slack_connected))
