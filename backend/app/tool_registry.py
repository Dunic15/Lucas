"""Org-scoped registry for Laura's own tools.

At meeting start Laura snapshots which capabilities this org and avatar can
actually use.  The model sees this same snapshot on every turn, so it never has
to guess whether a requested action is executable.

This registry does not discover or invoke an external Cedric service.  Google,
Asana, Slack, and future adapters are registered in ``native_runtime`` and run
inside Laura after canonical approval.  Slack remains an optional control and
message surface, not an execution owner.
"""
from __future__ import annotations

import time
from typing import Any

MAX_BRIEF_CHARS = 700

_BUILTINS = [
    {
        "name": "queue_action",
        "does": "capture any requested task for owner approval",
        "kind": "native",
        "write": True,
        "approval": "approve",
    },
    {
        "name": "calculator",
        "does": "arithmetic",
        "kind": "native",
        "write": False,
        "approval": "auto",
    },
    {
        "name": "date_math",
        "does": "date calculations",
        "kind": "native",
        "write": False,
        "approval": "auto",
    },
    {
        "name": "lookup_record",
        "does": "look up demo account records",
        "kind": "native",
        "write": False,
        "approval": "auto",
    },
    {
        "name": "upcoming_meetings",
        "does": "the owner's upcoming calendar",
        "kind": "native",
        "write": False,
        "approval": "auto",
    },
]


def _family_allowed(org_id: str, avatar: Any, family: str) -> bool:
    """Apply the same per-avatar narrowing used again at execution time."""
    from . import avatar_resolver, store

    avatar_id = str(getattr(avatar, "id", "") or "")
    explicit = store.get_avatar_capabilities(avatar_id).get(family)
    if explicit is False:
        return False

    if family == "asana":
        declares = bool(
            getattr(avatar, "uses_native_tool", lambda _name: False)("asana")
        )
        if explicit is not True and not declares:
            return False

    try:
        return bool(avatar_resolver.family_allowed(org_id, avatar_id, family))
    except Exception:  # noqa: BLE001 - overlay lookup never blocks a join
        return True


def assemble(org_id: str, avatar: Any) -> dict | None:
    """Build the connection-aware Laura tool snapshot for one meeting."""
    from . import native_runtime

    try:
        reg: dict = {
            "generated_at": time.time(),
            "native": [dict(tool) for tool in _BUILTINS],
            "disabled_families": [],
        }
        disabled: set[str] = set()
        for tool in native_runtime.catalog(org_id):
            family = str(tool.get("family") or "")
            if family and not _family_allowed(org_id, avatar, family):
                disabled.add(family)
                continue
            entry = dict(tool)
            entry["does"] = {
                "calendar.create_event": "schedule meetings on the owner's Google Calendar",
                "email.send": "send email as the owner through Gmail",
                "asana.create_task": "create tasks in the team's Asana workspace",
                "asana.update_task": "update tasks in the team's Asana workspace",
                "asana.add_comment": "comment on tasks in the team's Asana workspace",
                "slack.post_message": "post a message to the connected Slack channel",
            }.get(str(tool.get("type") or ""), str(tool.get("name") or "tool action"))
            entry["name"] = str(tool.get("type") or tool.get("name") or "")
            entry["label"] = str(tool.get("name") or entry["name"])
            reg["native"].append(entry)
        reg["disabled_families"] = sorted(disabled)

        # Compatibility-only empty fields: old live-tool code reads these keys.
        # Keeping them empty disables all external Cedric discovery/calls without
        # forcing a simultaneous rewrite of the latency-critical tool dispatcher.
        reg["cedric"] = {"connected": [], "available": [], "not_linked": False}
        reg["cedric_mcp"] = []

        reg["knowledge"] = {
            "docs": True,
            "drive_folder": bool(getattr(avatar, "drive_folder_id", "")),
        }
        return reg
    except Exception:  # noqa: BLE001 - a registry issue never blocks a join
        return None


def _unique_adapter_labels(reg: dict, *, connected: bool) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for tool in reg.get("native") or []:
        if "connected" not in tool or bool(tool.get("connected")) != connected:
            continue
        label = str(tool.get("label") or tool.get("name") or "").strip()
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def brief(reg: dict | None) -> str:
    """Compact per-turn prompt block describing only truthful capabilities."""
    if not reg:
        return ""
    connected = _unique_adapter_labels(reg, connected=True)
    disconnected = _unique_adapter_labels(reg, connected=False)
    disabled = [str(x) for x in (reg.get("disabled_families") or [])]

    lines = ["[YOUR LAURA TOOLS — this meeting]"]
    lines.append(
        "Built in: capture requested tasks for approval; calculator; date math; "
        "upcoming-calendar read."
    )
    if connected:
        lines.append(
            "Connected native tools, executed by Laura after owner approval: "
            + ", ".join(connected[:8])
            + "."
        )
    if disconnected:
        lines.append(
            "Not connected — never promise execution: "
            + ", ".join(disconnected[:8])
            + "."
        )
    if disabled:
        lines.append(
            "Disabled for this avatar by the owner: " + ", ".join(disabled[:6]) + "."
        )

    knowledge = reg.get("knowledge") or {}
    lines.append(
        "You can read: indexed company/process documents"
        + (" and the shared Drive folder brief" if knowledge.get("drive_folder") else "")
        + "."
    )
    lines.append(
        "Honesty: actions are captured and then approved; say 'queued for approval', "
        "never claim a write already happened during the meeting."
    )
    return "\n".join(lines)[:MAX_BRIEF_CHARS]


def search(reg: dict | None, query: str) -> str:
    """Search the in-memory meeting snapshot; no network on the live path."""
    q = str(query or "").strip().lower()
    if not reg:
        return "capability list unavailable for this session"
    if not q:
        return brief(reg) or "capability list unavailable for this session"

    hits: list[str] = []
    for tool in reg.get("native") or []:
        haystack = " ".join(
            str(tool.get(key) or "")
            for key in ("name", "label", "does", "family", "type")
        ).lower()
        if q not in haystack:
            continue
        state = ""
        if "connected" in tool:
            state = "connected" if tool.get("connected") else "NOT connected"
        mode = (
            "runs after owner approval"
            if tool.get("approval") == "approve"
            else "instant"
        )
        label = str(tool.get("label") or tool.get("name") or "tool")
        hits.append(
            f"{label} — Laura native"
            + (f" ({state})" if state else "")
            + f"; {mode}"
        )
    for family in reg.get("disabled_families") or []:
        if q in str(family).lower():
            hits.append(f"{family} — disabled for this avatar by the owner")
    if not hits:
        return (
            f"no Laura-native tool matches '{query}'. Capture the task for approval "
            "only if the owner can connect or add the required adapter."
        )
    return "; ".join(hits[:5])
