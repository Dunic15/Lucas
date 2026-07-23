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

import re

from typing import Any

ACTION_STATUSES = (
    "needs_details",
    "proposed",
    "approved",
    "executing",
    "rejected",
    "withdrawn",
    "done",
    "failed",
)
TERMINAL_STATUSES = ("rejected", "withdrawn", "done", "failed")

_STATUS_ALIASES = {
    "executed": "done",
    "completed": "done",
    "complete": "done",
    "declined": "rejected",
    "cancelled": "withdrawn",
    "canceled": "withdrawn",
    "discarded": "withdrawn",
    "error": "failed",
}


def normalize_status(status: str) -> str:
    """Return the canonical lifecycle state for a surface-reported value."""
    state = str(status or "").strip().lower()
    return _STATUS_ALIASES.get(state, state)


_FIELD = dict

# ── THE canonical vocabulary (Phase 1, owner 2026-07-22) ─────────────────────
# One row per parameter: wire name (`name`), the VOICE clarify slot that maps
# to it (`slot`, matching tools.missing_action_details' keys), and the human
# labels both surfaces speak/render. Before this, the voice asked for
# "quando" while the card said "start" — two vocabularies for one field.
PARAMS_SCHEMAS: dict[str, list[dict[str, Any]]] = {
    "calendar.create_event": [
        _FIELD(name="title", type="string", required=True,
               label="title", label_it="titolo"),
        _FIELD(
            name="start",
            type="string",
            required=True,
            description="ISO 8601 start",
            slot="invite_when", label="start time", label_it="orario",
        ),
        _FIELD(
            name="end",
            type="string",
            required=True,
            description="ISO 8601 end",
            label="end time", label_it="orario di fine",
        ),
        _FIELD(
            name="attendees",
            type="array",
            required=False,
            description="attendee emails",
            slot="invite_with", label="attendees", label_it="invitati",
        ),
        _FIELD(name="description", type="string", required=False,
               label="description", label_it="descrizione"),
    ],
    "calendar.update_event": [
        _FIELD(name="event_id", type="string", required=True,
               label="event ID", label_it="ID evento"),
        _FIELD(name="title", type="string", required=False,
               label="title", label_it="titolo"),
        _FIELD(name="start", type="string", required=False,
               description="ISO 8601 start", label="start time", label_it="orario"),
        _FIELD(name="end", type="string", required=False,
               description="ISO 8601 end", label="end time", label_it="orario di fine"),
        _FIELD(name="description", type="string", required=False,
               label="description", label_it="descrizione"),
    ],
    "email.send": [
        _FIELD(
            name="to",
            type="array",
            required=True,
            description="recipient emails",
            slot="email_to", label="recipient", label_it="destinatario",
        ),
        _FIELD(name="subject", type="string", required=True,
               label="subject", label_it="oggetto"),
        _FIELD(name="body", type="string", required=True,
               slot="email_body", label="message text", label_it="testo"),
    ],
    "gmail.create_draft": [
        _FIELD(name="to", type="array", required=True,
               description="recipient emails", slot="email_to",
               label="recipient", label_it="destinatario"),
        _FIELD(name="subject", type="string", required=True,
               label="subject", label_it="oggetto"),
        _FIELD(name="body", type="string", required=True,
               slot="email_body", label="message text", label_it="testo"),
    ],
    "asana.create_task": [
        _FIELD(name="name", type="string", required=True,
               slot="task_name", label="task name", label_it="nome del task"),
        _FIELD(name="notes", type="string", required=False,
               slot="description", label="description", label_it="descrizione"),
        _FIELD(name="project", type="string", required=False,
               slot="project", label="project", label_it="progetto"),
        _FIELD(name="assignee", type="string", required=False,
               slot="owner", label="owner", label_it="assegnatario"),
        _FIELD(name="due_on", type="string", required=False,
               slot="due", label="due date", label_it="scadenza"),
        _FIELD(name="subtasks", type="array", required=False,
               description="subtask titles, one per entry",
               label="subtasks", label_it="sotto-attività"),
        _FIELD(name="dependencies", type="array", required=False,
               description="tasks this depends on (name or gid)",
               label="dependencies", label_it="dipendenze"),
        _FIELD(name="attachments", type="array", required=False,
               description="attachment URLs",
               label="attachments", label_it="allegati"),
    ],
    "asana.update_task": [
        _FIELD(
            name="task",
            type="string",
            required=True,
            description="task gid",
            label="task ID", label_it="ID attività",
        ),
        _FIELD(name="completed", type="boolean", required=False,
               label="completed", label_it="completata"),
        _FIELD(name="due_on", type="string", required=False,
               label="due date", label_it="scadenza"),
    ],
    "asana.add_comment": [
        _FIELD(
            name="task",
            type="string",
            required=True,
            description="task gid",
            label="task ID", label_it="ID attività",
        ),
        _FIELD(name="text", type="string", required=True,
               label="comment text", label_it="testo commento"),
    ],
    "slack.post_message": [
        _FIELD(
            name="text",
            type="string",
            required=True,
            description="message text to post to the connected Slack channel",
            label="message text", label_it="testo messaggio",
        ),
    ],
    # ── the 20-action expansion (owner GO 2026-07-22): 13 new types, all
    # deterministic proxy builders — modeled on Pipedream's most-used catalog
    # actions, no tool-calling tier required. Name-based targets (files,
    # events) resolve on the EXACTLY-ONE rule: zero or many matches fail
    # honestly with the candidates listed, never a guess. ──
    "asana.create_project": [
        _FIELD(name="name", type="string", required=True,
               label="project name", label_it="nome del progetto"),
        _FIELD(name="notes", type="string", required=False,
               label="description", label_it="descrizione"),
    ],
    "asana.add_subtask": [
        _FIELD(name="task", type="string", required=True,
               description="parent task gid",
               label="parent task ID", label_it="ID attività padre"),
        _FIELD(name="name", type="string", required=True,
               label="subtask name", label_it="nome sotto-attività"),
    ],
    "gmail.reply": [
        _FIELD(name="to", type="string", required=True,
               description="the sender whose LATEST email gets the reply",
               slot="email_to", label="recipient", label_it="destinatario"),
        _FIELD(name="body", type="string", required=True,
               slot="email_body", label="message text", label_it="testo"),
    ],
    "gmail.add_label": [
        _FIELD(name="from_email", type="string", required=True,
               description="label the LATEST email from this sender",
               label="sender", label_it="mittente"),
        _FIELD(name="label", type="string", required=True,
               label="label name", label_it="nome etichetta"),
    ],
    "gmail.archive": [
        _FIELD(name="from_email", type="string", required=True,
               description="archive the LATEST email from this sender",
               label="sender", label_it="mittente"),
    ],
    "calendar.cancel_event": [
        _FIELD(name="title", type="string", required=True,
               description="upcoming event title (must match exactly one)",
               label="event title", label_it="titolo evento"),
        _FIELD(name="date", type="string", required=False,
               description="YYYY-MM-DD, to disambiguate",
               label="date", label_it="data"),
    ],
    "calendar.add_attendees": [
        _FIELD(name="title", type="string", required=True,
               label="event title", label_it="titolo evento"),
        _FIELD(name="attendees", type="array", required=True,
               description="attendee emails to add", slot="invite_with",
               label="attendees", label_it="invitati"),
    ],
    "calendar.rsvp": [
        _FIELD(name="title", type="string", required=True,
               label="event title", label_it="titolo evento"),
        _FIELD(name="response", type="string", required=True,
               description="accepted | declined | tentative",
               label="response", label_it="risposta"),
    ],
    "drive.share_file": [
        _FIELD(name="file", type="string", required=True,
               description="file name (must match exactly one)",
               label="file name", label_it="nome file"),
        _FIELD(name="email", type="string", required=True,
               label="share with (email)", label_it="condividi con (email)"),
        _FIELD(name="role", type="string", required=False,
               description="reader (default) | writer",
               label="permission", label_it="permesso"),
    ],
    "drive.create_folder": [
        _FIELD(name="name", type="string", required=True,
               label="folder name", label_it="nome cartella"),
        _FIELD(name="parent", type="string", required=False,
               description="parent folder name",
               label="inside folder", label_it="dentro la cartella"),
    ],
    "drive.create_doc": [
        _FIELD(name="name", type="string", required=True,
               label="document name", label_it="nome documento"),
        _FIELD(name="parent", type="string", required=False,
               description="parent folder name",
               label="inside folder", label_it="dentro la cartella"),
    ],
    "drive.rename_file": [
        _FIELD(name="file", type="string", required=True,
               label="file name", label_it="nome file"),
        _FIELD(name="name", type="string", required=True,
               label="new name", label_it="nuovo nome"),
    ],
    "drive.move_file": [
        _FIELD(name="file", type="string", required=True,
               label="file name", label_it="nome file"),
        _FIELD(name="folder", type="string", required=True,
               label="destination folder", label_it="cartella di destinazione"),
    ],
}

RISK_BY_TYPE: dict[str, str] = {
    "calendar.create_event": "low",
    "calendar.update_event": "medium",
    "email.send": "medium",
    "gmail.create_draft": "low",
    "asana.create_task": "low",
    "asana.update_task": "low",
    "asana.add_comment": "low",
    "slack.post_message": "medium",
    "asana.create_project": "low",
    "asana.add_subtask": "low",
    "gmail.reply": "medium",       # sends mail as the owner
    "gmail.add_label": "low",
    "gmail.archive": "low",
    "calendar.cancel_event": "high",   # destructive + notifies attendees
    "calendar.add_attendees": "medium",
    "calendar.rsvp": "low",
    "drive.share_file": "medium",  # grants access
    "drive.create_folder": "low",
    "drive.create_doc": "low",
    "drive.rename_file": "low",
    "drive.move_file": "low",
}


# Voice-slot → human label, DERIVED from the canonical rows above (plus the
# task-kind slots that exist only conversationally). Consumed by the live
# clarify lines and by any surface that names a missing slot — one vocabulary.
def _slot_labels(key: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for fields in PARAMS_SCHEMAS.values():
        for f in fields:
            if f.get("slot") and f.get(key):
                out.setdefault(str(f["slot"]), str(f[key]))
    return out


SLOT_LABELS_EN: dict[str, str] = {
    **_slot_labels("label"),
    # conversational phrasings the clarify line speaks aloud
    "owner": "who should own it",
    "project": "which project it goes in",
    "due": "when it's due",
    "description": "anything the description should say",
    "email_to": "who it should go to",
    "email_to_confirm": "please confirm the proposed email address",
    "email_body": "what it should say",
    "invite_with": "who should be on it",
    "invite_when": "when it should be",
}
SLOT_LABELS_IT: dict[str, str] = {
    **_slot_labels("label_it"),
    "owner": "chi la prende in carico",
    "project": "in quale progetto va",
    "due": "per quando serve",
    "description": "cosa scrivere nella descrizione",
    "email_to": "a chi va mandata",
    "email_to_confirm": "conferma l’indirizzo email proposto",
    "email_body": "cosa deve dire",
    "invite_with": "chi va invitato",
    "invite_when": "per quando fissarlo",
}


def needed_labels(keys: list[str], lang: str = "en") -> list[str]:
    """Human labels for missing keys — accepts BOTH wire param names and voice
    slots, so every surface (chips, forms, clarify) says the same words."""
    param_labels: dict[str, str] = {}
    lk = "label_it" if lang == "it" else "label"
    for fields in PARAMS_SCHEMAS.values():
        for f in fields:
            if f.get(lk):
                param_labels.setdefault(str(f["name"]), str(f[lk]))
    slots = SLOT_LABELS_IT if lang == "it" else SLOT_LABELS_EN
    out: list[str] = []
    for k in keys or []:
        k = str(k)
        out.append(param_labels.get(k) or slots.get(k) or k)
    return out


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


_TASK_NAME_PLACEHOLDERS = {
    "task", "new task", "create task", "create a task", "asana task",
    "new asana task", "create asana task", "create a new task",
}


def meaningful_task_name(value: Any) -> bool:
    """A provider-required task title must name the work, not the UI shell."""
    name = " ".join(str(value or "").split()).strip(" .?!").lower()
    if not name or name in _TASK_NAME_PLACEHOLDERS:
        return False
    # Model echoes of the request itself are placeholders too.
    return not bool(re.fullmatch(
        r"(?:please\s+)?(?:can\s+you\s+)?(?:create|make|add|open)\s+"
        r"(?:a\s+|an\s+|the\s+)?(?:new\s+)?(?:asana\s+)?task"
        r"(?:\s+(?:in|on)\s+asana)?", name
    ))


def missing_params(typed: dict | None) -> list[str]:
    """Required fields still missing or semantically placeholder-only."""
    schema = params_schema(typed)
    if not schema:
        return []
    args = typed.get("args") if isinstance(typed.get("args"), dict) else {}
    action_type = str(typed.get("type") or "")
    missing = [
        str(field["name"])
        for field in schema
        if field.get("required") and _empty(args.get(field["name"]))
    ]
    if (
        action_type == "asana.create_task"
        and "name" not in missing
        and not meaningful_task_name(args.get("name"))
    ):
        missing.insert(0, "name")
    return missing


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
