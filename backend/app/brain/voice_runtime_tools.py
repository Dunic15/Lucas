"""Tool adapters for the ElevenLabs runtime.

These functions expose the read capabilities Laura already has to the separate
ElevenLabs client-tool surface.  Every call is session/org scoped, validates the
runtime capability contract first, and returns a structured truth value instead
of asking the language model to infer connection state.
"""
from __future__ import annotations

import json
from typing import Any

from . import runtime_capabilities


def capability_contract(session: Any, avatar: Any) -> dict:
    return runtime_capabilities.build(session, avatar)


def capabilities_result(session: Any, avatar: Any, question: str = "") -> dict:
    contract = capability_contract(session, avatar)
    return {
        "summary": runtime_capabilities.spoken_answer(question, contract),
        "contract": contract,
    }


def _has_live(contract: dict, capability_id: str) -> bool:
    return any(
        str(item.get("id") or "") == capability_id
        for item in contract.get("live_now") or []
        if isinstance(item, dict)
    )


def _calendar_event(item: dict) -> dict:
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    attendees = [
        str(a.get("displayName") or a.get("email") or "").strip()
        for a in (item.get("attendees") or [])
        if isinstance(a, dict) and not a.get("self") and not a.get("resource")
    ]
    return {
        "title": str(item.get("summary") or "(no title)")[:200],
        "start": str(start.get("dateTime") or start.get("date") or ""),
        "end": str(end.get("dateTime") or end.get("date") or ""),
        "attendees": [a for a in attendees if a][:6],
        "meet_link": str(item.get("hangoutLink") or ""),
    }


def read_calendar(session: Any, avatar: Any, params: dict | None = None) -> dict:
    """Read the calendar now, preferring the zero-latency join snapshot.

    If the snapshot is empty, perform the same native-first/Pipedream-second read
    used at join.  This distinguishes an empty calendar from a disconnected or
    temporarily failing connector — the old ``upcoming_meetings`` string could
    not make that distinction.
    """
    params = params if isinstance(params, dict) else {}
    contract = capability_contract(session, avatar)
    if not _has_live(contract, "calendar.read"):
        return {
            "status": "unavailable",
            "source": "calendar",
            "note": "Google Calendar is not connected or enabled for this meeting.",
        }

    query = " ".join(str(params.get("query") or "").lower().split())[:120]
    try:
        maximum = max(1, min(int(params.get("max_results") or 8), 12))
    except (TypeError, ValueError):
        maximum = 8

    snapshot = str(getattr(session, "calendar_brief", "") or "").strip()
    if snapshot and not query and not params.get("refresh"):
        return {
            "status": "ready",
            "source": "meeting_start_snapshot",
            "summary": snapshot,
            "note": "Answer this calendar question now from the snapshot.",
        }

    from ..integrations import google_client

    org_id = str(getattr(session, "org_id", "") or "")
    result = google_client.list_calendar_events(org_id, max_results=maximum)
    if not result.get("ok"):
        try:
            from .. import pipedream_executor

            result = pipedream_executor.read_calendar_events(
                org_id, max_results=maximum
            )
        except Exception as exc:  # noqa: BLE001 — return a truthful soft failure
            result = {"ok": False, "error": type(exc).__name__}

    if not result.get("ok"):
        return {
            "status": "temporarily_unavailable",
            "source": "calendar",
            "note": (
                "Google Calendar is connected, but the live read failed right now: "
                + str(result.get("error") or "unknown read error")[:180]
            ),
        }

    events = [
        _calendar_event(item)
        for item in (result.get("events") or [])
        if isinstance(item, dict)
    ]
    if query:
        events = [
            item
            for item in events
            if query in item["title"].lower()
            or any(query in a.lower() for a in item.get("attendees") or [])
        ]
    events = events[:maximum]
    return {
        "status": "ready",
        "source": "live_calendar_read",
        "events": events,
        "note": (
            "Answer now from these events."
            if events
            else "The calendar read succeeded and no matching upcoming meetings were found."
        ),
    }


def read_inbox(session: Any, avatar: Any, params: dict | None = None) -> dict:
    """Read recent Gmail headers live (sender/subject/unread; never bodies)."""
    params = params if isinstance(params, dict) else {}
    contract = capability_contract(session, avatar)
    if not _has_live(contract, "gmail.inbox.read"):
        return {
            "status": "unavailable",
            "source": "gmail",
            "note": "Gmail is not connected or enabled for this meeting.",
        }
    try:
        maximum = max(1, min(int(params.get("max_results") or 8), 12))
    except (TypeError, ValueError):
        maximum = 8
    query = " ".join(str(params.get("query") or "").lower().split())[:120]

    from ..integrations import google_client

    org_id = str(getattr(session, "org_id", "") or "")
    result = google_client.list_inbox_messages(org_id, max_results=maximum)
    if not result.get("ok"):
        try:
            from .. import pipedream_executor

            result = pipedream_executor.read_gmail_inbox(
                org_id, max_results=maximum
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": type(exc).__name__}
    if not result.get("ok"):
        return {
            "status": "temporarily_unavailable",
            "source": "gmail",
            "note": (
                "Gmail is connected, but the inbox read failed right now: "
                + str(result.get("error") or "unknown read error")[:180]
            ),
        }

    messages = []
    for message in result.get("messages") or []:
        if not isinstance(message, dict):
            continue
        item = {
            "from": str(message.get("from") or "")[:240],
            "subject": str(message.get("subject") or "(no subject)")[:240],
            "unread": bool(message.get("unread")),
        }
        hay = f"{item['from']} {item['subject']}".lower()
        if not query or query in hay:
            messages.append(item)
        if len(messages) >= maximum:
            break
    return {
        "status": "ready",
        "source": "live_inbox_headers",
        "messages": messages,
        "privacy": "headers only; no email bodies were read",
        "note": (
            "Answer now from these inbox headers."
            if messages
            else "The inbox read succeeded and no matching messages were found."
        ),
    }


def read_asana(session: Any, avatar: Any, params: dict | None = None) -> dict:
    """Read Asana through the existing deterministic adapters.

    ``overview`` may use the bounded/cached workspace brief.  Current projects,
    tasks and search require ``session.asana_live``; writes never pass here.
    """
    params = params if isinstance(params, dict) else {}
    contract = capability_contract(session, avatar)
    if not _has_live(contract, "asana.read"):
        return {
            "status": "unavailable",
            "source": "asana",
            "note": "Asana has no readable workspace view in this meeting.",
        }

    operation = str(params.get("operation") or "overview").strip().lower()
    from . import tools as brain_tools
    from ..integrations import asana_client

    org_id = str(getattr(session, "org_id", "") or "")
    if operation == "overview":
        summary = str(asana_client.workspace_brief(org_id) or "").strip()
        return {
            "status": "ready" if summary else "temporarily_unavailable",
            "source": "asana_workspace_overview",
            "summary": summary,
            "note": (
                "Answer now from this overview."
                if summary
                else "Asana is connected, but no workspace overview was available."
            ),
        }

    if not bool(getattr(session, "asana_live", False)):
        return {
            "status": "not_live",
            "source": "asana",
            "note": "This meeting has an Asana overview, but current task reads are not enabled live.",
        }

    if operation == "projects":
        raw = brain_tools.asana_projects(session=session)
    elif operation == "tasks":
        raw = brain_tools.asana_tasks(
            project=str(params.get("project") or ""), session=session
        )
    elif operation == "search":
        raw = brain_tools.asana_search(
            query=str(params.get("query") or ""), session=session
        )
    else:
        return {
            "status": "error",
            "note": "operation must be overview, projects, tasks or search",
        }

    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 — tool errors are plain strings by contract
        parsed = None
    return {
        "status": "ready" if parsed is not None else "temporarily_unavailable",
        "source": f"asana_{operation}",
        "result": parsed if parsed is not None else raw,
        "note": "Answer now from this result." if parsed is not None else str(raw)[:240],
    }


def call_live_connector(
    session: Any, avatar: Any, params: dict | None = None
) -> dict:
    """Invoke one discovered MCP connector tool only when the manifest allows it."""
    params = params if isinstance(params, dict) else {}
    tool_name = str(params.get("tool_name") or "").strip()
    arguments = params.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    contract = capability_contract(session, avatar)
    allowed = runtime_capabilities.find_live_connector(contract, tool_name)
    if allowed is None:
        return {
            "status": "unavailable",
            "note": "That connector tool is not an approved live, read-only capability for this meeting.",
        }

    from .. import cedric_mcp

    meta = {
        "actor": "avatar",
        "source": "meeting",
        "ref": str(getattr(session, "bot_id", "") or ""),
    }
    result = cedric_mcp.call_tool(
        str(getattr(session, "org_id", "") or ""),
        tool_name,
        arguments,
        meta=meta,
        live=True,
    )
    if result.get("approval_required"):
        # Defense in depth: a tool catalog that changed after join must not turn
        # a supposedly read-only live call into a write.
        return {
            "status": "blocked",
            "note": "The connector changed to an approval-gated operation; nothing was executed.",
        }
    if result.get("ok"):
        return {
            "status": "ready",
            "source": f"connector:{tool_name}",
            "text": str(result.get("text") or "")[:6000],
            "structured": result.get("structured") or {},
            "note": "Answer now from this connector result.",
        }
    return {
        "status": "temporarily_unavailable",
        "source": f"connector:{tool_name}",
        "note": str(
            result.get("text")
            or result.get("error_kind")
            or "connector read failed"
        )[:240],
    }
