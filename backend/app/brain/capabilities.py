"""Deterministic, org-scoped capability truth for live answers.

Live 2026-07-23: within ONE meeting Petra said she was "connected to your Asana,
Jira, Calendar, Gmail and Drive", then "I don't have access to your Asana
account", then "I can't access external applications at all", and web-searched
"which actions can you do in Gmail?". The language model was inventing her own
capabilities every turn.

This module assembles ONE capability snapshot from the SAME sources the
dashboard and executors use — the join-cached tool_registry (connection state +
per-avatar enablement + verbs, assembled once at session start),
executor.route_for_typed (execution route), and the session's join-cached
asana_live flag (whether a workspace snapshot actually loaded). Live answers
read it directly and speak a deterministic sentence; the model never sees or
invents these values.

For every tool it separates FOUR states the model kept conflating:
  1. connected_for_org      — an org connector is active
  2. enabled_for_avatar     — this avatar is permitted to use it
  3. snapshot_available…    — a readable snapshot/retrieval is present THIS call
  4. can_execute_now        — a write can actually run
So the honest answer is "Asana is connected and I can create tasks, but I don't
have a workspace snapshot loaded to read existing tasks" — never the flat, wrong
"I don't have access to Asana".
"""
from __future__ import annotations

import re
import time
from typing import Any

# A representative typed action per tool family, used only to resolve the
# execution route (native vs pipedream) the same way the executor would.
_REP_TYPE: dict[str, str] = {
    "google_calendar": "calendar.create_event",
    "gmail_send": "email.send",
    "google_drive": "drive.share_file",
    "asana_tasks": "asana.create_task",
}

# Which families currently carry a per-MEETING readable snapshot. Only Asana
# does today (workspace_brief cached at join → session.asana_live); Calendar
# reads live and Drive/Gmail have no in-room inventory, so their "read" state is
# "on demand", never a fabricated snapshot.
_SNAPSHOT_FAMILIES = {"asana_tasks"}

# Spoken display names — never a raw registry key.
_DISPLAY = {
    "google_calendar": "Google Calendar",
    "gmail_send": "Gmail",
    "google_drive": "Google Drive",
    "asana_tasks": "Asana",
    "calculator": "a calculator",
    "date_math": "date math",
    "web_search": "web search",
    "lookup_record": "a demo record lookup",
}

# ── capability-question classifier ──────────────────────────────────────
# These must resolve to the deterministic capability answer BEFORE any public
# web-search routing (live 2026-07-23: "which action can you do in Gmail?" ran a
# Google web search). Broad on purpose: any "what/which … you … do/use/access
# … <tool>", "are you connected to X", "can you read/search my X", "do you have
# a snapshot of my X". A false positive only means an honest capability answer
# instead of a web search — safe.
# A CONCRETE app the question can name (so a bare "search the web" never counts).
_APPS = (
    r"asana|jira|gmail|e-?mail|mail|calendar|calendario|google|drive|slack|"
    r"notion|github|hubspot|linear|stripe|salesforce"
)
# Nouns that make a "what/which … you …" turn a self/roster question.
_CAP_NOUN = (
    r"tools?|integrations?|apps?|connectors?|actions?|capabilit\w+|functions?|"
    r"connections?|strumenti|integrazioni|azioni|funzion\w+|capacit\w+"
)
_CAPABILITY_Q = re.compile(
    r"(?:"
    # "what/which TOOLS/actions/connections can/do you use/have/access"
    r"(?:what|which|quali|che)\b[\w\s'’,]{0,20}?\b(?:" + _CAP_NOUN + r")\b"
    r"[\w\s'’,]{0,20}?\b(?:you|laura|petra|puoi|hai|usi)\b"
    r"|"
    # "what/which action(s) can you do/perform in <app>"  (app named)
    r"(?:what|which)\b[\w\s'’,]{0,30}?\b(?:can|could|do|are)\b[\w\s'’,]{0,16}?"
    r"\b(?:you|laura|petra)\b[\w\s'’,]{0,24}?\b(?:" + _APPS + r")\b"
    r"|"
    # "are you connected …", "do you have access …", "your connections"
    r"\b(?:are|is)\s+(?:you|laura|petra)\b[\w\s'’,]{0,20}?\bconnect\w*\b"
    r"|\b(?:do|does)\s+(?:you|laura|petra)\b[\w\s'’,]{0,20}?\baccess\b"
    r"|\b(?:your|laura'?s|petra'?s)\s+(?:actual\s+)?connect\w*\b"
    r"|"
    # "can you read/see/search/access … <APP>" — the app noun is REQUIRED so
    # "can you search online for the news" (no app) stays a normal web query.
    r"\b(?:can|could|do|will)\s+(?:you|laura|petra)\b[\w\s'’,]{0,20}?"
    r"\b(?:read|see|access|search|use|write|create|hear)\b[\w\s'’,]{0,24}?"
    r"\b(?:" + _APPS + r")\b"
    r"|"
    # "do you have a snapshot of my Asana / workspace"
    r"\bsnapshot\b[\w\s'’,]{0,20}?\b(?:" + _APPS + r"|workspace)\b"
    r"|"
    # Italian connection check
    r"\b(?:sei|siete)\s+(?:collegat\w+|conness\w+)\b"
    r")",
    re.IGNORECASE,
)

# Which single app the question is about (for a focused answer), else None → the
# full roster.
_FOCUS = [
    ("asana_tasks", re.compile(r"\basana\b", re.I)),
    ("google_drive", re.compile(r"\b(google\s+)?drive\b", re.I)),
    ("gmail_send", re.compile(r"\b(gmail|e-?mail|mail)\b", re.I)),
    ("google_calendar", re.compile(r"\bcalendar\b|\bcalendario\b", re.I)),
]


# ── context-bound ASR repair for mis-heard app names ────────────────────
# Recall's ASR mangles app names ("Asana" → "a zone"/"the zone", "Jira" →
# "gera"). Repair ONLY when the app was clearly named in recent conversation —
# "what's in my zone at the moment?" right after an Asana discussion is almost
# certainly "what's in my Asana?" (live 2026-07-23). NEVER a global rewrite:
# "zone"/"drive" keep their ordinary meaning unless that app was just discussed.
_ASR_CONFUSIONS: list[tuple[str, "re.Pattern[str]"]] = [
    ("Asana", re.compile(r"\b(?:a\s+zone|the\s+zone|my\s+zone|az[ao]na|us[ao]na|"
                          r"a\s+sauna|savannah)\b", re.I)),
    ("Jira", re.compile(r"\b(?:gera|gira|jeera)\b", re.I)),
]


def repair_asr(text: str, history: str) -> str:
    """Return `text` with a likely-mangled app name repaired, but ONLY when that
    app appears in recent `history`. Conservative and reversible: with no
    supporting context the text is returned unchanged (the caller may then ask
    one short clarification rather than guess)."""
    t = text or ""
    hist = history or ""
    for app, rx in _ASR_CONFUSIONS:
        if rx.search(t) and re.search(rf"\b{re.escape(app)}\b", hist, re.I):
            t = rx.sub(app, t)
    return t


def is_capability_question(text: str) -> bool:
    """True when this asks what the avatar can do / is connected to. Answered
    from the deterministic snapshot, never web-searched or model-invented."""
    return bool(_CAPABILITY_Q.search(text or ""))


def _focus_app(text: str) -> str | None:
    t = text or ""
    # Google umbrella ("what can you do in Google?") → the Google trio, no single
    # focus, so the answer covers Calendar + Gmail + Drive.
    if re.search(r"\bgoogle\b", t, re.I) and not re.search(
        r"\bdrive\b|\bcalendar\b|\bgmail\b", t, re.I
    ):
        return "google"
    for name, rx in _FOCUS:
        if rx.search(t):
            return name
    return None


# ── the deterministic truth object ──────────────────────────────────────
def snapshot(avatar: Any, org_id: str, session: Any = None) -> dict:
    """One org-scoped capability truth object. Never touches the language model.

    tools[name] = {connected_for_org, enabled_for_avatar, live_health,
    snapshot_available_in_meeting, can_read_now, can_execute_now,
    execution_route, supported_verbs, unavailable_reason}.
    """
    from . import tool_registry
    from ..actions import executor

    # Reuse the registry assembled ONCE at join (session.tool_registry) — the
    # SAME object tools.py reads. Re-assembling here per question would repeat a
    # networked catalog GET on the hot path and, on a transient failure, flip the
    # answer to "nothing connected" (live 2026-07-23). `ok` marks whether a
    # registry was actually available, so cached_snapshot never caches/serves a
    # void one. (An earlier build referenced a non-existent `tool_registry.build`
    # and silently fell back to empty every call — fixed 2026-07-23.)
    ok = True
    reg = getattr(session, "tool_registry", None) if session is not None else None
    if reg is None:
        # No join-cached registry (stale/failed session): build once, best-effort.
        try:
            reg = tool_registry.assemble(org_id, avatar)
        except Exception:  # noqa: BLE001 — a registry hiccup must never break the turn
            reg = None
    if not reg:
        reg = {"native": []}
        ok = False

    asana_live = bool(getattr(session, "asana_live", False)) if session else False
    tools: dict[str, dict] = {}
    for e in reg.get("native", []):
        name = str(e.get("name") or "")
        if not name:
            continue
        # Built-ins (calculator/date_math/web_search) have no 'connected' key and
        # are always available; integrations carry an explicit connected bool.
        has_conn = "connected" in e
        connected = bool(e.get("connected", True))
        writeable = bool(e.get("write", False))
        # The registry stores `verbs` as the comma-joined PHRASE (tool_registry
        # uses family_verbs_text), so list() would split the string into single
        # characters — the "I can a, d, d" bug heard live 2026-07-23. Split a
        # string on commas; accept a real list unchanged.
        _verbs_raw = e.get("verbs")
        if isinstance(_verbs_raw, str):
            verbs = [v.strip() for v in _verbs_raw.split(",") if v.strip()]
        else:
            verbs = [str(v).strip() for v in (_verbs_raw or []) if str(v).strip()]
        route = ""
        rep = _REP_TYPE.get(name)
        if rep and connected:
            try:
                route = executor.route_for_typed({"type": rep}, org_id)
            except Exception:  # noqa: BLE001
                route = ""
        # snapshot state: only families that carry a per-meeting inventory
        if name in _SNAPSHOT_FAMILIES:
            snap: bool | None = asana_live
        else:
            snap = None  # no in-room snapshot concept (reads on demand / live)
        reason = ""
        if has_conn and not connected:
            reason = "not connected for this org"
        elif name in _SNAPSHOT_FAMILIES and connected and not asana_live:
            reason = "connected, but no workspace snapshot loaded for this meeting"
        tools[name] = {
            "connected_for_org": connected,
            # tool_registry already folds the per-avatar toggle into 'connected'
            # (asana_on gates on avatar.uses_native_tool + capability_enabled), so
            # for the surfaced tools connected == enabled_for_avatar.
            "enabled_for_avatar": connected,
            "live_health": "ok" if connected else "unconfigured",
            "snapshot_available_in_meeting": snap,
            "can_read_now": (bool(snap) if snap is not None else connected),
            "can_execute_now": connected and writeable,
            "execution_route": route or ("native" if connected else ""),
            "supported_verbs": verbs,
            "unavailable_reason": reason,
        }
    return {"generated_at": time.time(), "tools": tools, "ok": ok}


def cached_snapshot(avatar: Any, org_id: str, session: Any = None) -> dict:
    """A per-session-stable capability snapshot.

    Connection state, per-avatar enablement, routes and verbs don't change
    within a meeting, yet the underlying reads (tool_registry, executor) can
    transiently fail and momentarily report "nothing connected" — which the
    live answer then speaks as fact. This memoises the FIRST good snapshot on
    the session and reuses it for the rest of the meeting, so capability answers
    stay consistent. It recomputes only when the snapshot-availability signal
    (`asana_live`, which flips False→True once a workspace brief loads at join)
    changes, and it NEVER overwrites a good cache with a failed/empty build.

    Trade-off (accepted): a tool connected *mid-meeting* via the dashboard won't
    surface until asana_live changes or the session ends — connections rarely
    change mid-call, and killing the flip-flop is the priority. Falls back to a
    live `snapshot()` when there is no session to cache on.
    """
    if session is None:
        return snapshot(avatar, org_id, session)

    live = bool(getattr(session, "asana_live", False))
    cached = getattr(session, "_capability_snapshot", None)
    cached_live = getattr(session, "_capability_snapshot_live", None)
    if cached and cached.get("tools") and cached_live == live:
        return cached

    fresh = snapshot(avatar, org_id, session)
    if fresh.get("ok") and fresh.get("tools"):
        session._capability_snapshot = fresh
        session._capability_snapshot_live = live
        return fresh
    # Build failed or came back empty: prefer the last good snapshot over
    # speaking a false "nothing is connected".
    if cached and cached.get("tools"):
        return cached
    return fresh


# ── deterministic spoken answer ─────────────────────────────────────────
def _one_tool_line(name: str, s: dict) -> str:
    disp = _DISPLAY.get(name, name)
    if not s["connected_for_org"]:
        return f"{disp} isn't connected for this workspace, so I can't use it here."
    route = s["execution_route"]
    where = (
        " through Pipedream" if route == "pipedream"
        else " through this workspace's connected account" if route == "native"
        else ""
    )
    verbs = s["supported_verbs"]
    can_do = (", ".join(verbs[:4]) if verbs else "use it")
    line = f"{disp} is connected{where} and I can {can_do}."
    # The read/snapshot distinction is the honest part the model kept getting
    # wrong: connected ≠ a loaded snapshot to read from.
    if s["snapshot_available_in_meeting"] is False:
        line += (
            f" I don't have a snapshot of your {disp} workspace loaded for this "
            "meeting yet, so I can't read the existing items right now — I can pull "
            "one or capture a new item for approval."
        )
    return line


def answer(text: str, snap: dict) -> str:
    """A deterministic spoken capability answer from the snapshot. No model.

    Focused on the app the question names when it names one, else a short roster.
    Only states connected/permitted/snapshot/executable — never invents an app
    (no phantom Jira) and never flip-flops.
    """
    tools = snap.get("tools") or {}
    focus = _focus_app(text)

    if focus == "google":
        parts = [
            _one_tool_line(n, tools[n])
            for n in ("google_calendar", "gmail_send", "google_drive")
            if n in tools
        ]
        return " ".join(parts) or "I don't have Google connected for this workspace."

    if focus and focus in tools:
        return _one_tool_line(focus, tools[focus])

    # Full roster: name only what is actually connected, plus the always-on
    # built-ins, so she never claims an app she isn't connected to.
    connected = [
        _DISPLAY.get(n, n)
        for n, s in tools.items()
        if s["connected_for_org"] and n in _REP_TYPE
    ]
    if connected:
        joined = (
            connected[0] if len(connected) == 1
            else ", ".join(connected[:-1]) + " and " + connected[-1]
        )
        tail = ""
        # Surface the one honest caveat if a connected snapshot tool has no snap.
        for n, s in tools.items():
            if s["connected_for_org"] and s["snapshot_available_in_meeting"] is False:
                tail = (
                    f" I don't have a snapshot of your {_DISPLAY.get(n, n)} workspace "
                    "loaded for this meeting yet, so I can't read existing items now."
                )
                break
        return (
            f"For this workspace I'm connected to {joined}. I can capture actions "
            "for your approval and, once approved, run the ones that are "
            "connected." + tail
        )
    return (
        "No workspace apps are connected here yet — I can still answer questions "
        "and capture actions for approval, and you can connect a tool in the "
        "dashboard so I can run them."
    )
