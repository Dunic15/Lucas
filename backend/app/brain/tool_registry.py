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
    {"name": "upcoming_meetings", "does": "this workspace's upcoming calendar (read)",
     "kind": "native", "write": False, "approval": "auto"},
]

# Human labels for the executor's app families — the one place a slug becomes
# words, so the meeting brief, the Connections cards and the System Check board
# all name a tool the same way.
FAMILY_LABELS: dict[str, str] = {
    "google_calendar": "Google Calendar",
    "gmail": "Gmail",
    "google_drive": "Google Drive",
    "asana": "Asana",
}


def family_verbs(slug: str) -> list[str]:
    """What the EXECUTOR can actually run for one app family, derived from
    pipedream_executor._MAPPER — the single catalog.

    This is the same derivation the in-meeting capability brief uses (#377):
    the avatar's self-knowledge, the Connections cards and the System Check
    board all read the execution plane itself, so none of them can ever claim
    a verb the executor lost (or miss one it gained). Never raises — an
    unavailable executor simply yields no verbs.
    """
    try:
        from .. import pipedream_executor as _pe

        return sorted(
            t.split(".", 1)[1].replace("_", " ")
            for t, spec in _pe._MAPPER.items()
            if spec[0] == slug
        )
    except Exception:  # noqa: BLE001 — never block a join/board on this
        return []


def family_verbs_text(slug: str) -> str:
    """`family_verbs` as the comma-joined phrase the spoken brief uses."""
    return ", ".join(family_verbs(slug))


# Families surfaced as their own native rows below — never duplicated into the
# generic connected-app bucket.
_NATIVE_ROW_SLUGS = frozenset(
    {"slack", "asana", "google", "gmail", "google_calendar", "google_drive"}
)


def org_connected_apps(org_id: str) -> dict[str, list[str]]:
    """The ORG's connected-app catalog: ``{app_slug: [executable verbs]}``.

    Sourced from ``app_policy.catalog`` — the ONE place that turns "which
    accounts did this org actually connect" into a capability answer. The
    avatar's self-knowledge, OpenClaw Chat and the proxy executor therefore
    read the same catalog, so none of them can claim a capability another one
    would refuse.

    This exists because the avatar's self-knowledge used to come from somewhere
    else entirely: a hardcoded Gmail/Calendar/Drive/Asana list plus whichever
    Pipedream apps the owner had EXPLICITLY toggled on for that avatar. A
    workspace whose Notion OpenClaw can run was therefore invisible to the
    avatar, which then told the room Notion was not connected (live
    2026-07-29). One catalog, one answer.

    The verbs are derived from the executor's own mapper plus the app registry,
    so nothing here needs Pipedream's separately priced pre-built action
    catalog. An app the org connected but for which Laura has no registered API
    surface still appears with no verbs — present and honestly bounded, never
    denied.

    SYNC + best-effort: called once at session start (never on the live path),
    and any failure yields ``{}`` so a join is never blocked by a catalog GET.
    """
    org = str(org_id or "").strip()
    if not org:
        return {}
    try:
        from ..actions import app_policy

        return {
            entry["slug"]: app_policy.verbs_for(
                entry["slug"], entry["deterministic_types"]
            )
            for entry in app_policy.catalog(org)
            if entry.get("slug")
        }
    except Exception:  # noqa: BLE001 — never block a join over the catalog
        return {}


def org_connected_app_limits(org_id: str) -> dict[str, str]:
    """``{slug: limit}`` for connected apps whose capability is bounded.

    Only apps with a REAL limit appear, so the brief stays quiet about the
    ordinary case and the avatar can name what it cannot do instead of
    promising a generic API call it would then fail to make.
    """
    org = str(org_id or "").strip()
    if not org:
        return {}
    try:
        from ..actions import app_policy

        return {
            entry["slug"]: entry["limit"]
            for entry in app_policy.catalog(org)
            if entry.get("slug") and entry.get("limit")
        }
    except Exception:  # noqa: BLE001 — never block a join over the catalog
        return {}


def _connected_app_buckets(
    org_id: str, avatar: Any
) -> tuple[list[dict], list[str]]:
    """Split the org's connected-app catalog into what THIS avatar may use and
    what the workspace has but the avatar is not enabled for.

    The per-avatar switch follows ``store.capability_enabled``: an explicit
    ``False`` blocks, and an app nobody ever toggled defaults to the org's
    connected state. The previous rule demanded an explicit ``True``, so an
    untouched avatar in a workspace with Notion connected saw nothing at all —
    and denying a connected app is worse than either honest answer.
    """
    from .. import store

    avatar_id = str(getattr(avatar, "id", "") or "")
    limits = org_connected_app_limits(org_id)
    enabled_apps: list[dict] = []
    org_available: list[str] = []
    for slug, verbs in sorted(org_connected_apps(org_id).items()):
        if slug in _NATIVE_ROW_SLUGS:
            continue
        try:
            allowed = store.capability_enabled(
                avatar_id, slug, connected=True, org_id=org_id
            )
        except Exception:  # noqa: BLE001 — a toggle read never blocks the join
            allowed = True
        if allowed:
            entry = {
                "slug": slug,
                "actions": list(verbs or family_verbs(slug))[:4],
            }
            # Only a REAL limit is carried. An empty one says nothing and stays
            # out of the brief, which rides in every live turn under a cap.
            if limits.get(slug):
                entry["limit"] = limits[slug]
            enabled_apps.append(entry)
        elif slug not in org_available:
            org_available.append(slug)
    return enabled_apps[:6], org_available[:6]


def assemble(org_id: str, avatar: Any) -> dict | None:
    """Build the org-scoped registry. SYNC + network (one Cedric catalog GET) —
    call via run_in_threadpool at session start only. Best-effort: any failure
    returns what could be gathered; a totally failed assembly returns None and
    the join proceeds exactly as today."""
    # Lazy imports keep the module graph cycle-free (store/cedric import
    # neither this module nor each other's heavy parts at load time).
    from .. import store
    from ..cedric import callback as cedric_callback

    try:
        reg: dict = {"generated_at": time.time(), "native": list(_BUILTINS)}

        # Google (Gmail + Calendar). Post-cutover the org connects these in
        # Pipedream (managed OAuth), so count a Pipedream-connected account too —
        # otherwise the avatar would tell people "Google isn't connected" in a
        # meeting even though it can execute. Native OAuth still counts (fallback).
        google_on = False
        try:
            google_on = bool(store.get_org_oauth(org_id, provider="google"))
        except Exception:  # noqa: BLE001 — absence of a token is not an error
            google_on = False
        try:
            from .. import pipedream_executor

            if not google_on and pipedream_executor.enabled():
                google_on = (
                    pipedream_executor.app_connected(org_id, "gmail")
                    or pipedream_executor.app_connected(org_id, "google_calendar")
                )
        except Exception:  # noqa: BLE001 — Pipedream absence is not an error
            pass
        # Live capability awareness (owner ask 2026-07-22): the verb list for
        # each family is DERIVED from the executor's own registry — the
        # avatar's self-knowledge can never lag the execution plane again.
        _family_verbs = family_verbs_text

        reg["native"].append({
            "name": "google_calendar",
            "does": ("on this workspace's Google Calendar: "
                     + (_family_verbs("google_calendar") or "schedule meetings")),
            "kind": "native", "write": True, "approval": "approve", "connected": google_on,
            "verbs": _family_verbs("google_calendar"),
        })
        reg["native"].append({
            "name": "gmail_send",
            "does": ("as this workspace's Gmail: "
                     + (_family_verbs("gmail") or "send email")),
            "kind": "native", "write": True, "approval": "approve", "connected": google_on,
            "verbs": _family_verbs("gmail"),
        })
        # Google Drive actions are Pipedream-only (no native plane): shown
        # connected only when the org linked google_drive in Connect.
        drive_on = False
        try:
            from .. import pipedream_executor as _pe2

            drive_on = _pe2.enabled() and _pe2.app_connected(org_id, "google_drive")
        except Exception:  # noqa: BLE001
            drive_on = False
        reg["native"].append({
            "name": "google_drive",
            "does": ("in the owner's Drive: "
                     + (_family_verbs("google_drive") or "share and organize files")),
            "kind": "native", "write": True, "approval": "approve", "connected": drive_on,
            "verbs": _family_verbs("google_drive"),
        })

        # Native Asana — Petra-only: the org is connected AND this avatar is
        # purpose-built for Asana (avatar.native_tools, e.g. Petra) AND the
        # per-avatar toggle isn't off. Other avatars never see it in their tool
        # brief even when the org has connected Asana. Google Calendar/Gmail
        # above stay baseline for every avatar.
        asana_on = False
        try:
            from .. import asana_client, pipedream_executor

            asana_connected = asana_client.connected(org_id) or (
                pipedream_executor.enabled()
                and pipedream_executor.app_connected(org_id, "asana")
            )
            if asana_connected:
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
                "does": ("in the team's Asana workspace: "
                         + (_family_verbs("asana")
                            or "create and update tasks")),
                "kind": "native", "write": True, "approval": "approve",
                "connected": asana_on,
                "verbs": _family_verbs("asana"),
            })

        # Generic connected apps (Notion / GitHub / Jira / HubSpot / …) — from
        # the ORG's connected-app catalog, so the avatar knows IN CONVERSATION
        # what it can capture and can never deny an app the execution plane can
        # actually run (live 2026-07-29). An app the owner explicitly toggled
        # off for this avatar lands in pd_org_available instead: present but
        # not promised, which is honest either way.
        pd_apps: list[dict] = []
        pd_org_available: list[str] = []
        try:
            pd_apps, pd_org_available = _connected_app_buckets(org_id, avatar)
            # Pipedream's pre-built action NAMES read better than the derived
            # verbs when its (separately priced) catalog is available. Purely
            # cosmetic: the verb fallback keeps the app listed either way.
            try:
                from .. import pipedream_client, pipedream_executor

                if pipedream_executor.enabled():
                    for app in pd_apps:
                        names = [
                            str(a.get("name") or "")
                            for a in pipedream_client.list_actions(
                                app["slug"], limit=5
                            )
                        ]
                        names = [n for n in names if n]
                        if names:
                            app["actions"] = names[:4]
            except Exception:  # noqa: BLE001 — paid catalog is optional
                pass
        except Exception:  # noqa: BLE001 — never block a join over the catalog
            pd_apps = pd_apps or []
        reg["pd_apps"] = pd_apps
        reg["pd_org_available"] = pd_org_available[:6]

        # Cedric connectors — only when this org has a connected Slack agent.
        cedric_reg: dict = {
            "connected": [], "available": [], "not_linked": False, "linked": False
        }
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
        # Truthfulness gates (live incident 2026-07-21 — the avatar claimed it
        # could use Slack in an org whose Slack agent wasn't actually usable):
        # 1. require the row's OWN team_id — a stale 'connected' row with an
        #    empty config must NOT trigger an org-only catalog fetch that can
        #    resolve to another workspace's connectors;
        # 2. a not_linked (or malformed) catalog yields NO claims at all —
        #    never fall back to whatever the stale row said.
        if team_id:
            # Slack itself is connected to the org even when Cedric's connector
            # catalog is empty. Preserve that distinction for self-questions:
            # "connected to the org, runs through Cedric", never "not connected".
            cedric_reg["linked"] = True
            data = cedric_callback.fetch_org_connectors(org_id, team_id)
            if isinstance(data, dict):
                if data.get("not_linked"):
                    cedric_reg["not_linked"] = True
                else:
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

        # THE PER-AVATAR SLACK TOGGLE IS ENFORCED HERE, at snapshot time (off
        # the hot path): everything Cedric-flavored — the connector brief AND
        # the callable MCP tools — runs via the Slack agent, so an avatar whose
        # `slack` capability the owner explicitly toggled OFF gets none of it.
        # The avatar's brief says so (see brief()), so when someone asks it to
        # use Slack/a Cedric tool it answers honestly that the toggle is off
        # instead of pretending or silently failing. Same "explicit False
        # blocks" rule as the approve doors; an untouched avatar is unchanged.
        slack_blocked = False
        try:
            aid = str(getattr(avatar, "id", "") or "")
            if aid and store.get_avatar_capabilities(
                aid, org_id
            ).get("slack") is False:
                slack_blocked = True
        except Exception:  # noqa: BLE001 — never block a join over the toggle
            slack_blocked = False
        reg["slack_blocked"] = slack_blocked

        # Callable Cedric tools via the MCP bridge (Handshake contract v3) — the
        # display-only connector list above says WHAT exists; these are the tools
        # Laura can actually INVOKE (cedric_mcp.call_tool). Gated + best-effort +
        # off the hot path (session start). Empty unless CEDRIC_MCP_ENABLED and
        # Cedric returns a catalog, so join is byte-identical when the flag is off.
        reg["cedric_mcp"] = []
        try:
            from .. import cedric_mcp

            if cedric_mcp.enabled() and not slack_blocked:
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
    drive_on = any(
        t.get("name") == "google_drive" and t.get("connected") for t in native
    )

    def _verbs_of(name: str) -> str:
        for t in native:
            if t.get("name") == name and t.get("verbs"):
                return str(t["verbs"])
        return ""

    ced = reg.get("cedric") or {}
    connected = [t["name"] for t in (ced.get("connected") or [])][:8]
    available = [str(n) for n in (ced.get("available") or [])][:6]
    lines = ["[YOUR TOOLS — this meeting]"]
    # Verb lists come from the EXECUTOR's own registry (assemble derives them
    # from the action mapper) — what she claims is exactly what can run.
    cal_v = _verbs_of("google_calendar") or "create event"
    gm_v = _verbs_of("gmail_send") or "send"
    dr_v = _verbs_of("google_drive") or "share file"
    as_v = _verbs_of("asana_tasks") or "create task"
    lines.append(
        "Native: capture any requested task for approval (queue_action); "
        + (f"Calendar ({cal_v}) + Gmail ({gm_v}) — connected, run after owner approval; "
           if google_on else
           "Google Calendar + Gmail NOT connected (owner can connect in the dashboard); ")
        + (f"Asana ({as_v}) — connected; " if asana_on else "")
        + (f"Drive ({dr_v}) — connected; " if drive_on else
           "Drive NOT connected (owner can connect it in Connections); ")
        + "calculator; date math."
    )
    if reg.get("slack_blocked"):
        # The owner toggled Slack OFF for this avatar: no Slack-agent tools are
        # offered, and the avatar must say so plainly when asked.
        lines.append(
            "Slack agent: DISABLED for you — the owner toggled it off. If "
            "asked to use Slack or any Slack-agent tool, say you can't because "
            "it isn't toggled on for you; never pretend or work around it."
        )
    else:
        if ced.get("linked"):
            lines.append(
                "Slack: connected to this org; it runs through Cedric after "
                "owner approval."
            )
        if connected:
            lines.append(
                "Enabled via the Slack agent (captured, then run after owner "
                "approval): " + ", ".join(connected) + "."
            )
        if available:
            lines.append(
                "NOT connected (never promise these): " + ", ".join(available) + "."
            )
    # Generic Pipedream apps this avatar may use (Notion / GitHub / Jira / …),
    # with a few example actions so the avatar can speak to them concretely.
    for app in (reg.get("pd_apps") or [])[:4]:
        nm = str(app.get("slug", "")).replace("_", " ").title()
        acts = ", ".join(app.get("actions") or [])
        lines.append(
            f"{nm} (connected via Pipedream — captured then run after owner approval)"
            + (f": e.g. {acts}." if acts else ".")
        )
    # Org-connected apps NOT enabled for this avatar: name them honestly as
    # present-but-off so the avatar neither denies them nor promises them.
    org_avail = [
        str(s).replace("_", " ").title() for s in (reg.get("pd_org_available") or [])
    ]
    if org_avail:
        lines.append(
            "Connected in your org but NOT enabled for you (the owner can "
            "toggle them on): " + ", ".join(org_avail) + " — don't promise these."
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
    blocked = bool(reg.get("slack_blocked"))
    for t in ced.get("connected") or []:
        if q in str(t.get("name", "")).lower():
            if blocked:
                hits.append(
                    f"{t['name']} — via the Slack agent, but Slack is toggled "
                    "OFF for you by the owner; say you can't use it"
                )
            else:
                hits.append(
                    f"{t['name']} — via the Slack agent (connected); runs after owner approval"
                    + ("; needs reconnect" if t.get("needs_reconnect") else "")
                )
    for n in ced.get("available") or []:
        if q in str(n).lower():
            hits.append(
                f"{n} — via the Slack agent but "
                + ("Slack is toggled OFF for you; say you can't use it"
                   if blocked else "NOT connected; do not promise it")
            )
    if not hits and blocked and "slack" in q:
        hits.append(
            "Slack agent — toggled OFF for you by the owner; if asked, say you "
            "can't use Slack because it isn't toggled on"
        )
    if (
        not hits
        and not blocked
        and ced.get("linked")
        and "slack" in q
    ):
        hits.append(
            "Slack — connected to this org; runs through Cedric after owner approval"
        )
    for app in reg.get("pd_apps") or []:
        slug = str(app.get("slug") or "")
        hay = " ".join([slug, *[str(a) for a in app.get("actions") or []]]).lower()
        if q in hay:
            hits.append(
                f"{slug.replace('_', ' ').title()} — connected and enabled; "
                "captured, then runs after owner approval"
            )
    for slug in reg.get("pd_org_available") or []:
        if q in str(slug).lower():
            hits.append(
                f"{str(slug).replace('_', ' ').title()} — connected for this "
                "workspace but NOT enabled for this avatar"
            )
    if not hits:
        return (
            f"no tool matches '{query}'. If asked to do this, capture it with "
            "queue_action and say it will need the owner to set the tool up."
        )
    return "; ".join(hits[:5])
