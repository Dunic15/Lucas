"""Canonical Action Control Plane vocabulary (M0).

One module owns what every approval surface and executor must agree on:

- the parameter schema for each typed action the platform can execute
  (``params_schema``) and which fields are required — the deterministic
  source of the ``needs_details`` state;
- the risk class per action type;
- normalization of peer-reported statuses at the boundary (Cedric says
  ``executed``; the canonical vocabulary says ``done``);
- the deterministic execution idempotency key shared with Cedric.

Nothing here calls the network or the database, and nothing here ever sees
transcript content — typed args are the already-distilled safe parameters.
Docs: docs/product/UNIFIED-ACTION-CONTROL-PLANE.md.
"""
from __future__ import annotations

from typing import Any

# Canonical lifecycle states (superset of the pre-M0 vocabulary; the two new
# states are needs_details — typed spec incomplete, editable — and executing —
# an execution claim is held). Terminal states are unchanged.
ACTION_STATUSES = (
    "needs_details", "proposed", "approved", "executing",
    "rejected", "done", "failed",
)
TERMINAL_STATUSES = ("rejected", "done", "failed")

# Peer/report-side aliases normalized at the boundary — inbound only, never
# emitted. Cedric's historical vocabulary says "executed" for a completed run.
_STATUS_ALIASES = {
    "executed": "done",
    "completed": "done",
    "complete": "done",
    "declined": "rejected",
    "error": "failed",
}


def normalize_status(status: str) -> str:
    """Canonical status for a peer-reported one ('' when empty/unknown-shaped
    input; unknown non-empty values pass through for the caller's 400)."""
    st = str(status or "").strip().lower()
    return _STATUS_ALIASES.get(st, st)


# ── typed parameter schemas ─────────────────────────────────────────────────
# One entry per typed action the platform executes natively today. Field names
# match exactly what executor.from_typed feeds google_client / asana_client —
# the schema documents the REAL wire shape, it never invents one.
_FIELD = dict  # readability alias for the literal rows below

PARAMS_SCHEMAS: dict[str, list[dict[str, Any]]] = {
    "calendar.create_event": [
        _FIELD(name="title", type="string", required=True),
        _FIELD(name="start", type="string", required=True,
               description="ISO 8601 start"),
        _FIELD(name="end", type="string", required=True,
               description="ISO 8601 end"),
        _FIELD(name="attendees", type="array", required=False,
               description="attendee emails"),
        _FIELD(name="description", type="string", required=False),
    ],
    "email.send": [
        _FIELD(name="to", type="array", required=True,
               description="recipient emails"),
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
        _FIELD(name="task", type="string", required=True,
               description="task gid"),
        _FIELD(name="completed", type="boolean", required=False),
        _FIELD(name="due_on", type="string", required=False),
    ],
    "asana.add_comment": [
        _FIELD(name="task", type="string", required=True,
               description="task gid"),
        _FIELD(name="text", type="string", required=True),
    ],
}

# Risk class per action type (the approval surfaces render this; browser/skill
# steps will extend the same vocabulary). External communication is never
# "low": an email leaves the tenant's blast radius, a calendar event or a task
# in the team's own tools does not.
RISK_BY_TYPE: dict[str, str] = {
    "calendar.create_event": "low",
    "email.send": "medium",
    "asana.create_task": "low",
    "asana.update_task": "low",
    "asana.add_comment": "low",
}


def params_schema(typed: dict | None) -> list[dict[str, Any]]:
    """The parameter schema for a typed spec's type ([] for untyped/unknown —
    unknown types stay on the free-text Cedric card path exactly as today)."""
    if not isinstance(typed, dict):
        return []
    return [dict(f) for f in PARAMS_SCHEMAS.get(str(typed.get("type") or ""), [])]


def risk_for(typed: dict | None) -> str:
    """Risk class for a typed spec ('' when untyped/unknown)."""
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
    """Required fields the typed spec does not carry — non-empty means the
    action is ``needs_details`` (approving it would execute a broken call or
    silently no-op, the 2026-07-16 'approved but nothing happened' failure)."""
    schema = params_schema(typed)
    if not schema:
        return []
    args = typed.get("args") if isinstance(typed.get("args"), dict) else {}
    return [
        str(f["name"]) for f in schema
        if f.get("required") and _empty(args.get(f["name"]))
    ]


# Editable value shapes per schema field type — everything else is refused so
# a surface can never smuggle nested structures into a typed spec.
_PARAM_TYPES = {"string": str, "boolean": bool, "array": list}


def validate_param_edits(
    schema: list[dict], args: dict
) -> tuple[dict, list[str]]:
    """(cleaned_args, errors) — unknown fields and wrong shapes are errors.
    Values are trimmed and bounded; arrays are string-only and capped."""
    by_name = {str(f.get("name")): f for f in schema}
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
                isinstance(v, str) for v in value
            ):
                errors.append(f"{key} must be an array of strings")
                continue
            cleaned[key] = [v.strip()[:300] for v in value][:20]
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
    """The deterministic execution key both systems enforce: one external
    write per approved action (per chosen slot, for proposal actions). Carried
    on the durable row and sent to Cedric as Idempotency-Key."""
    aid = str(action_id or "").strip()
    if not aid:
        return ""
    slot = str(slot_id or "").strip()
    return f"exec:{aid}:{slot}" if slot else f"exec:{aid}"
