"""Org-scoped tool registry — what THIS avatar can actually do in THIS meeting.

The agent should never guess where a capability lives. At session start the
registry is assembled once (off the live path, best-effort — the same contract
as drive_client.folder_brief) and carried on the session; a compact brief is
folded into the same memory_brief channel the Drive folder uses, so every turn
the brain knows:

  - what it can do NATIVELY (Google Calendar/Gmail via the native executor —
    when the org's own Google is connected),
  - what runs VIA THE SLACK AGENT (Cedric) after owner approval — only the
    connectors that are actually CONNECTED for this org,
  - what is NOT connected (never promise those), and
  - the honesty rule: actions are captured → approved → executed, never "done"
    during the call.

Two session-aware brain tools (tools.list_capabilities / tools.search_tools)
read the SAME snapshot — zero network on the live path.

PII: tool names only — no accounts, no emails, no transcripts.
"""
from __future__ import annotations

import time
from typing import Any

# Hard cap on the injected brief. It rides in the live prompt on every turn, so
# it must stay cheap (~150 tokens). The compose loop below trims connector
# lists before it ever gets near the cap; this is the belt-and-braces.
MAX_BRIEF_CHARS = 700

# Native, always-available brain tools (tools.py) — kept in sync by the tests.
_BUILTINS = [
    {"name": "queue_action", "does": "capture any requested task for owner approval",
     "kind": "native", "write": True, "approval": "approve"},
    {"name": "calculator", "does": "arithmetic", "kind": "native", "write": False,
     "approval": "auto"},
    {"name": "date_math", "does": "date calculations", "kind": "native",
     "write": False, "approval": "auto"},
    {"name": "lookup_record", "does": "look up demo account records",
     "kind": "native", "write": False, "approval": "auto"},
    {"name": "upcoming_meetings", "does": "the owner's upcoming calendar (read)",
     "kind": "native", "write": False, "approval": "auto"},
]


def assemble(org_id: str, avatar: Any) -> dict | None:
    """Build the org-scoped registry. SYNC + network (one Cedric catalog GET) —
    call via run_in_threadpool at session start only. Best-effort: any failure
    returns what could be gathered; a totally failed assembly returns None and
    the join proceeds exactly as today."""
    # Lazy imports keep the module graph cycle-free (store/cedric import
    # neither this module nor each other's heavy parts at load time).
    from . import store
    from .cedric import callback as cedric_callback

    try:
        reg: dict = {"generated_at": time.time(), "native": list(_BUILTINS)}

        # Native Google (the org's OWN OAuth → the native executor).
        google_on = False
        try:
            google_on = bool(store.get_org_oauth(org_id, provider="google"))
        except Exception:  # noqa: BLE001 — absence of a token is not an error
            google_on = False
        reg["native"].append({
            "name": "google_calendar", "does": "schedule meetings on the owner's Google",
            "kind": "native", "write": True, "approval": "approve", "connected": google_on,
        })
        reg["native"].append({
            "name": "gmail_send", "does": "send email as the owner",
            "kind": "native", "write": True, "approval": "approve", "connected": google_on,
        })

        # Native Asana — Petra-only: the org is connected AND this avatar is
        # purpose-built for Asana (avatar.native_tools, e.g. Petra) AND the
        # per-avatar toggle isn't off. Other avatars never see it in their tool
        # brief even when the org has connected Asana. Google Calendar/Gmail
        # above stay baseline for every avatar.
        asana_on = False
        try:
            from . import asana_client

            if asana_client.connected(org_id):
                declares = bool(
                    getattr(avatar, "uses_native_tool", lambda _n: False)("asana")
                )
                asana_on = store.capability_enabled(
                    getattr(avatar, "id", ""), "asana", connected=declares
                )
        except Exception:  # noqa: BLE001 — absence of a token is not an error
            asana_on = False
        if asana_on:
            reg["native"].append({
                "name": "asana_tasks",
                "does": "create and update tasks in the team's Asana workspace",
                "kind": "native", "write": True, "approval": "approve",
                "connected": asana_on,
            })

        # Cedric connectors — only when this org has a connected Slack agent.
        cedric_reg: dict = {"connected": [], "available": [], "not_linked": False}
        team_id = ""
        try:
            rows = store.connections_for_org(org_id)
            brain = next(
                (r for r in rows
                 if r.get("provider") == "cedric-brain" and r.get("status") == "connected"),
                None,
            )
            if brain:
                team_id = str((brain.get("config") or {}).get("team_id") or "")
        except Exception:  # noqa: BLE001
            brain = None
        if team_id or brain:
            data = cedric_callback.fetch_org_connectors(org_id, team_id)
            if isinstance(data, dict):
                if data.get("not_linked"):
                    cedric_reg["not_linked"] = True
                for c in data.get("connectors") or []:
                    if not isinstance(c, dict):
                        continue
                    name = str(c.get("name") or c.get("key") or "").strip()
                    if not name:
                        continue
                    entry = {"name": name, "kind": "cedric", "write": True,
                             "approval": "approve",
                             "needs_reconnect": bool(c.get("needs_reconnect"))}
                    if c.get("connected"):
                        cedric_reg["connected"].append(entry)
                    else:
                        cedric_reg["available"].append(name)
        reg["cedric"] = cedric_reg

        # Callable Cedric tools via the MCP bridge (Handshake contract v3) — the
        # display-only connector list above says WHAT exists; these are the tools
        # Laura can actually INVOKE (cedric_mcp.call_tool). Gated + best-effort +
        # off the hot path (session start). Empty unless CEDRIC_MCP_ENABLED and
        # Cedric returns a catalog, so join is byte-identical when the flag is off.
        reg["cedric_mcp"] = []
        try:
            from . import cedric_mcp

            if cedric_mcp.enabled():
                mcp_tools = cedric_mcp.list_tools(org_id)
                if mcp_tools:
                    reg["cedric_mcp"] = mcp_tools
        except Exception:  # noqa: BLE001 — never block a join over the bridge
            pass

        # Knowledge sources (what it can READ — RAG + the Drive brief).
        reg["knowledge"] = {
            "docs": True,  # every avatar has an indexed knowledge folder
            "drive_folder": bool(getattr(avatar, "drive_folder_id", "")),
        }
        return reg
    except Exception:  # noqa: BLE001 — never block a join over the registry
        return None


def brief(reg: dict | None) -> str:
    """The compact per-turn prompt block. Hard-capped; tool names only."""
    if not reg:
        return ""
    native = reg.get("native") or []
    google_on = any(
        t.get("name") == "google_calendar" and t.get("connected") for t in native
    )
    asana_on = any(
        t.get("name") == "asana_tasks" and t.get("connected") for t in native
    )
    ced = reg.get("cedric") or {}
    connected = [t["name"] for t in (ced.get("connected") or [])][:8]
    available = [str(n) for n in (ced.get("available") or [])][:6]
    lines = ["[YOUR TOOLS — this meeting]"]
    lines.append(
        "Native: capture any requested task for approval (queue_action); "
        + ("Google Calendar + Gmail (connected — executed after owner approval); "
           if google_on else
           "Google Calendar + Gmail NOT connected (owner can connect in the dashboard); ")
        + ("Asana tasks (connected — executed after owner approval); "
           if asana_on else "")
        + "calculator; date math."
    )
    if connected:
        lines.append(
            "Via the Slack agent after owner approval: " + ", ".join(connected) + "."
        )
    if available:
        lines.append(
            "NOT connected (never promise these): " + ", ".join(available) + "."
        )
    know = reg.get("knowledge") or {}
    lines.append(
        "You can read: your indexed process docs"
        + ("; the shared Drive folder brief" if know.get("drive_folder") else "")
        + "."
    )
    lines.append(
        "Honesty: actions are CAPTURED then approved after the call — say "
        "\"queued for approval\", never claim something was already done."
    )
    text = "\n".join(lines)
    return text[:MAX_BRIEF_CHARS]


def search(reg: dict | None, query: str) -> str:
    """Keyword search over the snapshot — for the search_tools brain tool.
    No network; returns a short human-readable answer for the model."""
    q = (query or "").strip().lower()
    if not reg:
        return "capability list unavailable for this session"
    if not q:
        return brief(reg) or "capability list unavailable for this session"
    hits: list[str] = []
    for t in reg.get("native") or []:
        hay = f"{t.get('name','')} {t.get('does','')}".lower()
        if q in hay:
            state = ""
            if "connected" in t:
                state = " (connected)" if t.get("connected") else " (NOT connected)"
            hits.append(
                f"{t['name']} — native{state}; "
                + ("runs after owner approval" if t.get("approval") == "approve"
                   else "instant")
            )
    ced = reg.get("cedric") or {}
    for t in ced.get("connected") or []:
        if q in str(t.get("name", "")).lower():
            hits.append(
                f"{t['name']} — via the Slack agent (connected); runs after owner approval"
                + ("; needs reconnect" if t.get("needs_reconnect") else "")
            )
    for n in ced.get("available") or []:
        if q in str(n).lower():
            hits.append(f"{n} — via the Slack agent but NOT connected; do not promise it")
    if not hits:
        return (
            f"no tool matches '{query}'. If asked to do this, capture it with "
            "queue_action and say it will need the owner to set the tool up."
        )
    return "; ".join(hits[:5])
