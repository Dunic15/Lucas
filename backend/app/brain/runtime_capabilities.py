"""Runtime-truth contract for the ElevenLabs meeting agent.

The legacy Laura brain already receives a session-scoped tool registry and can call
its native tools directly.  The ElevenLabs runtime is a separate tool surface: a
connector being configured does *not* mean that Cedric can read it live, and a
write being executable does *not* mean it may run without approval.

This module turns the actual session snapshot into four explicit buckets:

* ``live_now``          — call the named client tool and answer during the call;
* ``approval_required`` — capture the write with ``queue_action``;
* ``not_live``          — connected, but no safe/fast live read path exists;
* ``unavailable``       — disconnected or disabled for this avatar.

The same object is injected into the per-call prompt and returned by the
capability tool.  The model therefore never has to infer capability state from a
persona, a stale connector list, or its own previous answer.
"""
from __future__ import annotations

import re
from typing import Any


_MAX_PER_BUCKET = 12


def _clean_words(values: Any, limit: int = 8) -> list[str]:
    if isinstance(values, str):
        raw = values.split(",")
    elif isinstance(values, (list, tuple, set)):
        raw = values
    else:
        raw = []
    out: list[str] = []
    for value in raw:
        text = " ".join(str(value or "").split()).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _tool_state(snapshot: dict, name: str) -> dict:
    tools = snapshot.get("tools") if isinstance(snapshot, dict) else {}
    value = (tools or {}).get(name)
    return value if isinstance(value, dict) else {}


def _entry(
    capability_id: str,
    label: str,
    *,
    tool: str = "",
    can: str = "",
    verbs: Any = None,
    route: str = "",
    reason: str = "",
    connector_tool: str = "",
) -> dict:
    item = {"id": capability_id, "label": label}
    if tool:
        item["tool"] = tool
    if connector_tool:
        item["connector_tool"] = connector_tool
    if can:
        item["can"] = can
    cleaned = _clean_words(verbs)
    if cleaned:
        item["verbs"] = cleaned
    if route:
        item["route"] = route
    if reason:
        item["reason"] = reason
    return item


def _append_unique(bucket: list[dict], item: dict) -> None:
    key = (str(item.get("id") or ""), str(item.get("connector_tool") or ""))
    for existing in bucket:
        if (
            str(existing.get("id") or ""),
            str(existing.get("connector_tool") or ""),
        ) == key:
            return
    if len(bucket) < _MAX_PER_BUCKET:
        bucket.append(item)


def build(session: Any, avatar: Any) -> dict:
    """Return the operational truth for this exact meeting session.

    No network calls occur here.  Every input was assembled at join and frozen on
    the session, so prompt injection and the later capability-tool call see the
    same answer for the duration of the meeting.
    """
    from . import capabilities
    from .. import cedric_mcp

    org_id = str(getattr(session, "org_id", "") or "")
    snapshot = capabilities.cached_snapshot(avatar, org_id, session)
    registry = getattr(session, "tool_registry", None)
    registry = registry if isinstance(registry, dict) else {}

    live_now: list[dict] = []
    approval_required: list[dict] = []
    not_live: list[dict] = []
    unavailable: list[dict] = []

    # Platform reads that are always part of the ElevenLabs client-tool surface.
    _append_unique(
        live_now,
        _entry(
            "meeting.context.read",
            "meeting context",
            tool="get_meeting_context",
            can="read the meeting purpose, participants, decisions and open items",
        ),
    )
    _append_unique(
        live_now,
        _entry(
            "company_knowledge.search",
            "company knowledge",
            tool="search_company_knowledge",
            can="search indexed company documents and past meeting knowledge",
        ),
    )

    # Google Calendar: reads are live; every mutation remains approval-gated.
    calendar = _tool_state(snapshot, "google_calendar")
    if calendar.get("connected_for_org"):
        _append_unique(
            live_now,
            _entry(
                "calendar.read",
                "Google Calendar",
                tool="get_upcoming_meetings",
                can="read upcoming meetings during this call",
            ),
        )
        if calendar.get("can_execute_now"):
            _append_unique(
                approval_required,
                _entry(
                    "calendar.write",
                    "Google Calendar",
                    tool="queue_action",
                    can="create, update, cancel or change calendar events after approval",
                    verbs=calendar.get("supported_verbs"),
                    route=str(calendar.get("execution_route") or ""),
                ),
            )
    else:
        _append_unique(
            unavailable,
            _entry(
                "calendar",
                "Google Calendar",
                reason=str(calendar.get("unavailable_reason") or "not connected for this workspace"),
            ),
        )

    # Gmail has a bounded header-only read path plus approval-gated mutations.
    gmail = _tool_state(snapshot, "gmail_send")
    if gmail.get("connected_for_org"):
        _append_unique(
            live_now,
            _entry(
                "gmail.inbox.read",
                "Gmail inbox",
                tool="get_inbox_summary",
                can="read recent inbox headers during this call (sender, subject, unread; never bodies)",
            ),
        )
        if gmail.get("can_execute_now"):
            _append_unique(
                approval_required,
                _entry(
                    "gmail.write",
                    "Gmail",
                    tool="queue_action",
                    can="draft, send, reply, label or archive after approval",
                    verbs=gmail.get("supported_verbs"),
                    route=str(gmail.get("execution_route") or ""),
                ),
            )
    else:
        _append_unique(
            unavailable,
            _entry(
                "gmail",
                "Gmail",
                reason=str(gmail.get("unavailable_reason") or "not connected for this workspace"),
            ),
        )

    # Asana reads are available only when the avatar is enabled for Asana.  The
    # read tool itself distinguishes a cached overview from native current-state
    # project/task/search operations.
    asana = _tool_state(snapshot, "asana_tasks")
    if asana.get("connected_for_org"):
        if bool(getattr(session, "asana_live", False)) or bool(
            getattr(session, "asana_brief_loaded", False)
        ):
            _append_unique(
                live_now,
                _entry(
                    "asana.read",
                    "Asana",
                    tool="read_asana",
                    can="read the workspace overview and, when live reads are enabled, projects and tasks",
                ),
            )
        else:
            _append_unique(
                not_live,
                _entry(
                    "asana.read",
                    "Asana",
                    reason="connected for actions, but no readable workspace view loaded in this meeting",
                ),
            )
        if asana.get("can_execute_now"):
            _append_unique(
                approval_required,
                _entry(
                    "asana.write",
                    "Asana",
                    tool="queue_action",
                    can="create or update task work after approval",
                    verbs=asana.get("supported_verbs"),
                    route=str(asana.get("execution_route") or ""),
                ),
            )

    # Drive writes are connected independently.  Arbitrary Drive inventory is
    # deliberately not advertised as a live read; indexed/shared-folder content
    # is reached through search_company_knowledge instead.
    drive = _tool_state(snapshot, "google_drive")
    if drive.get("connected_for_org") and drive.get("can_execute_now"):
        _append_unique(
            approval_required,
            _entry(
                "drive.write",
                "Google Drive",
                tool="queue_action",
                can="create, share, rename or move Drive items after approval",
                verbs=drive.get("supported_verbs"),
                route=str(drive.get("execution_route") or ""),
            ),
        )
        _append_unique(
            not_live,
            _entry(
                "drive.inventory.read",
                "Google Drive inventory",
                reason="no arbitrary live file-listing tool; search indexed company knowledge instead",
            ),
        )

    # Pipedream long-tail apps are real, enabled writes, but still approval-only.
    for app in registry.get("pd_apps") or []:
        if not isinstance(app, dict):
            continue
        slug = str(app.get("slug") or "").strip()
        if not slug:
            continue
        label = slug.replace("_", " ").replace("-", " ").title()
        _append_unique(
            approval_required,
            _entry(
                f"pipedream.{slug}.write",
                label,
                tool="queue_action",
                can="run a connected Pipedream action after approval",
                verbs=app.get("actions"),
                route="pipedream",
            ),
        )

    for slug in registry.get("pd_org_available") or []:
        text = str(slug or "").strip()
        if text:
            _append_unique(
                unavailable,
                _entry(
                    f"pipedream.{text}",
                    text.replace("_", " ").replace("-", " ").title(),
                    reason="connected for the workspace but disabled for this avatar",
                ),
            )

    cedric = registry.get("cedric") if isinstance(registry.get("cedric"), dict) else {}
    if registry.get("slack_blocked"):
        _append_unique(
            unavailable,
            _entry(
                "slack_agent",
                "Slack agent",
                reason="disabled for this avatar by the owner",
            ),
        )
    else:
        for connector in cedric.get("connected") or []:
            if not isinstance(connector, dict):
                continue
            name = str(connector.get("name") or "").strip()
            if name:
                _append_unique(
                    approval_required,
                    _entry(
                        f"cedric.{name.lower().replace(' ', '_')}.write",
                        name,
                        tool="queue_action",
                        can="run through the connected Slack agent after approval",
                        route="cedric",
                        reason="needs reconnect" if connector.get("needs_reconnect") else "",
                    ),
                )
        for name in cedric.get("available") or []:
            text = str(name or "").strip()
            if text:
                _append_unique(
                    unavailable,
                    _entry(
                        f"cedric.{text.lower().replace(' ', '_')}",
                        text,
                        reason="available through the Slack agent but not connected",
                    ),
                )

    # Cedric MCP discovery can expose arbitrary connector reads.  Only the MCP
    # contract's read-only + fast tools are callable live.  The agent invokes
    # them through one validated bridge tool so no unlisted tool can be smuggled.
    for tool in registry.get("cedric_mcp") or []:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        description = " ".join(str(tool.get("description") or name).split())[:240]
        annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        if cedric_mcp.is_live_safe(tool):
            _append_unique(
                live_now,
                _entry(
                    f"connector.{name}.read",
                    name.replace("_", " ").replace("-", " "),
                    tool="call_live_connector",
                    connector_tool=name,
                    can=description,
                ),
            )
        elif annotations.get("readOnlyHint"):
            _append_unique(
                not_live,
                _entry(
                    f"connector.{name}.read",
                    name.replace("_", " ").replace("-", " "),
                    reason="connected read tool, but too slow for the live meeting path",
                ),
            )
        else:
            _append_unique(
                approval_required,
                _entry(
                    f"connector.{name}.write",
                    name.replace("_", " ").replace("-", " "),
                    tool="queue_action",
                    can=description,
                    route="cedric_mcp",
                ),
            )

    return {
        "live_now": live_now,
        "approval_required": approval_required,
        "not_live": not_live,
        "unavailable": unavailable,
        "rules": {
            "live_now": "call the named tool now and answer from its result; never promise to do it later",
            "approval_required": "call queue_action and say it is waiting for approval; never claim it is done",
            "not_live": "say the live read is not available; do not invent a result or promise an unqueued follow-up",
            "unavailable": "say it is not connected or enabled for this avatar",
        },
    }


def _focus_terms(question: str) -> set[str]:
    q = " ".join((question or "").lower().split())
    aliases = {
        "calendar": {"calendar", "meeting", "meetings", "schedule", "calendario", "riunioni"},
        "gmail": {"gmail", "email", "mail", "inbox", "posta"},
        "asana": {"asana", "task", "tasks", "project", "progetto", "attività"},
        "drive": {"drive", "file", "folder", "document", "cartella", "documento"},
        "slack": {"slack"},
        "knowledge": {"knowledge", "documents", "docs", "company", "process", "documenti"},
    }
    focused: set[str] = set()
    for key, words in aliases.items():
        if any(re.search(rf"\b{re.escape(word)}\b", q) for word in words):
            focused.add(key)
    return focused


def _matches_focus(item: dict, focus: set[str]) -> bool:
    if not focus:
        return True
    hay = " ".join(
        str(item.get(k) or "")
        for k in ("id", "label", "tool", "connector_tool", "can", "reason")
    ).lower()
    return any(key in hay for key in focus)


def spoken_answer(question: str, contract: dict) -> str:
    """Short deterministic answer for "what can you do / is X connected?".

    It intentionally states execution mode, not just connection state.  That is
    the distinction the live model previously lost.
    """
    focus = _focus_terms(question)
    live = [x for x in contract.get("live_now") or [] if _matches_focus(x, focus)]
    approval = [
        x for x in contract.get("approval_required") or [] if _matches_focus(x, focus)
    ]
    not_live = [x for x in contract.get("not_live") or [] if _matches_focus(x, focus)]
    unavailable = [
        x for x in contract.get("unavailable") or [] if _matches_focus(x, focus)
    ]

    def labels(items: list[dict], limit: int = 6) -> str:
        vals: list[str] = []
        for item in items:
            label = str(item.get("label") or "").strip()
            if label and label not in vals:
                vals.append(label)
            if len(vals) >= limit:
                break
        return ", ".join(vals)

    parts: list[str] = []
    if live:
        parts.append(f"I can use {labels(live)} live during this meeting")
    if approval:
        parts.append(f"I can prepare actions in {labels(approval)} for approval")
    if not_live:
        parts.append(f"I cannot live-read {labels(not_live)} in this session")
    if unavailable:
        parts.append(f"{labels(unavailable)} is not connected or enabled here")
    if not parts:
        return "I don't have a matching connected capability in this meeting."
    return ". ".join(parts[:3]) + "."


def find_live_connector(contract: dict, tool_name: str) -> dict | None:
    """Return the manifest entry for an allowed live connector tool."""
    wanted = str(tool_name or "").strip()
    if not wanted:
        return None
    for item in contract.get("live_now") or []:
        if (
            item.get("tool") == "call_live_connector"
            and str(item.get("connector_tool") or "") == wanted
        ):
            return item
    return None
