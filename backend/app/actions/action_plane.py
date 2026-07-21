"""Canonical Action Control Plane vocabulary.

One module owns what every approval surface and Laura-native adapter agrees on:

* parameter schemas and required fields;
* risk classes;
* canonical lifecycle-state normalization; and
* deterministic execution idempotency keys.

Nothing here calls the network or database, and it never sees transcript content.
Typed args are already-distilled execution parameters.
"""
from __future__ import annotations

from typing import Any

ACTION_STATUSES = (
    "needs_details",
    "proposed",
    "approved",
    "executing",
    "rejected",
    "done",
    "failed",
)
TERMINAL_STATUSES = ("rejected", "done", "failed")

_STATUS_ALIASES = {
    "executed": "done",
    "completed": "done",
    "complete": "done",
    "declined": "rejected",
    "error": "failed",
}


def normalize_status(status: str) -> str:
    """Return the canonical lifecycle state for a surface-reported value."""
    state = str(status or "").strip().lower()
    return _STATUS_ALIASES.get(state, state)


_FIELD = dict

PARAMS_SCHEMAS: dict[str, list[dict[str, Any]]] = {
    "calendar.create_event": [
        _FIELD(name="title", type="string", required=True),
        _FIELD(
            name="start",
            type="string",
            required=True,
            description="ISO 8601 start",
        ),
        _FIELD(
            name="end",
            type="string",
            required=True,
            description="ISO 8601 end",
        ),
        _FIELD(
            name="attendees",
            type="array",
            required=False,
            description="attendee emails",
        ),
        _FIELD(name="description", type="string", required=False),
    ],
    "email.send": [
        _FIELD(
            name="to",
            type="array",
            required=True,
            description="recipient emails",
        ),
        _FIELD(name="subject", type="string", required=True),
        _FIELD(name="body", type="string", required=True),
    ],
    "asana.create_task": [
        _FIELD(name="name", type="string", required=True),
        _FIELD(name="notes", type="string", required=False),
        _FIELD(name="project", type="string", required=False),
        _FIELD(name="assignee", type="string", required=False),
        _FIELD(name="due_on", type="string", required=False),
    ],
    "asana.update_task": [
        _FIELD(
            name="task",
            type="string",
            required=True,
            description="task gid",
        ),
        _FIELD(name="completed", type="boolean", required=False),
        _FIELD(name="due_on", type="string", required=False),
    ],
    "asana.add_comment": [
        _FIELD(
            name="task",
            type="string",
            required=True,
            description="task gid",
        ),
        _FIELD(name="text", type="string", required=True),
    ],
    "slack.post_message": [
        _FIELD(
            name="text",
            type="string",
            required=True,
            description="message text to post to the connected Slack channel",
        ),
    ],
}

RISK_BY_TYPE: dict[str, str] = {
    "calendar.create_event": "low",
    "email.send": "medium",
    "asana.create_task": "low",
    "asana.update_task": "low",
    "asana.add_comment": "low",
    "slack.post_message": "medium",
}


def params_schema(typed: dict | None) -> list[dict[str, Any]]:
    """Return the real wire schema for a typed action, or an empty list."""
    if not isinstance(typed, dict):
        return []
    return [
        dict(field)
        for field in PARAMS_SCHEMAS.get(str(typed.get("type") or ""), [])
    ]


def risk_for(typed: dict | None) -> str:
    """Return the risk class for a typed action."""
    if not isinstance(typed, dict):
        return ""
    return RISK_BY_TYPE.get(str(typed.get("type") or ""), "")


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def missing_params(typed: dict | None) -> list[str]:
    """Required fields still missing from a typed action."""
    schema = params_schema(typed)
    if not schema:
        return []
    args = typed.get("args") if isinstance(typed.get("args"), dict) else {}
    return [
        str(field["name"])
        for field in schema
        if field.get("required") and _empty(args.get(field["name"]))
    ]


_PARAM_TYPES = {"string": str, "boolean": bool, "array": list}


def validate_param_edits(
    schema: list[dict], args: dict
) -> tuple[dict, list[str]]:
    """Validate and bound parameter edits made by an approval surface."""
    by_name = {str(field.get("name")): field for field in schema}
    cleaned: dict = {}
    errors: list[str] = []
    for key, value in args.items():
        field = by_name.get(str(key))
        if field is None:
            errors.append(f"unknown field {key!r}")
            continue
        expected = _PARAM_TYPES.get(str(field.get("type") or "string"), str)
        if expected is list:
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                errors.append(f"{key} must be an array of strings")
                continue
            cleaned[key] = [item.strip()[:300] for item in value][:20]
        elif expected is bool:
            if not isinstance(value, bool):
                errors.append(f"{key} must be a boolean")
                continue
            cleaned[key] = value
        else:
            if not isinstance(value, str):
                errors.append(f"{key} must be a string")
                continue
            cleaned[key] = value.strip()[:2000]
    return cleaned, errors


def execution_idempotency_key(action_id: str, slot_id: str = "") -> str:
    """Stable exactly-once key shared by every Laura approval surface."""
    aid = str(action_id or "").strip()
    if not aid:
        return ""
    slot = str(slot_id or "").strip()
    return f"exec:{aid}:{slot}" if slot else f"exec:{aid}"
