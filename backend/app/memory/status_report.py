"""Laura's weekly status report — the canonical PM one-pager, composed.

Deterministic assembly from the meeting-memory window (the same distilled
rows the week brief reads — never a transcript) plus the org's Asana
workspace brief when connected. Structure follows the standard PM status
report: overall RAG → executive summary → done this period → upcoming →
open risks → decisions needed → open actions.

The RAG value is a documented HEURISTIC estimate, not judgement:
  red   — any risk/summary mentions blocked/slipped/missed language;
  amber — open actions without an owner, or any risks at all;
  green — otherwise.

Flag-gated (settings.status_report_enabled) and read-only: composing never
writes anything; SENDING the report is the existing email approve door with
this body prefilled by the dashboard. Never raises — ``{"ok": False}`` soft
results only (finalize/dashboard callers must never break on it).
"""
from __future__ import annotations

import json
from typing import Any

from ..core.config import settings
from . import meeting_memory

_RED_RE_WORDS = ("blocked", "blocker", "slipped", "missed", "at risk",
                 "behind schedule", "delay")


def enabled() -> bool:
    return bool(settings.status_report_enabled) and meeting_memory.enabled()


def _rag(risks: list[str], actions: list[dict], summaries: list[str]) -> str:
    haystack = " ".join(risks + summaries).lower()
    if any(w in haystack for w in _RED_RE_WORDS):
        return "red"
    unowned = any(
        not str(a.get("owner") or "").strip()
        or str(a.get("owner") or "").upper() == "UNASSIGNED"
        for a in actions
    )
    if risks or unowned:
        return "amber"
    return "green"


def compose(org_id: str, avatar_id: str = "") -> dict:
    """Build the one-pager from the last 7 days of meeting memory.

    Returns ``{ok, report, source_meetings}`` where report is
    ``{rag, exec_summary, done, upcoming, risks, decisions_needed,
    actions_open}`` — or ``{ok: False, error}`` when the flag/plane is off or
    nothing is in the window."""
    try:
        if not enabled():
            return {"ok": False, "error": "status reports are not enabled"}
        with meeting_memory._engine().begin() as conn:
            meeting_memory._set_org(conn, org_id)
            rows = meeting_memory._window_rows(conn, org_id, avatar_id)
        if not rows:
            return {"ok": False, "error": "no meetings in the last 7 days"}

        summaries: list[str] = []
        decisions: list[str] = []
        risks: list[str] = []
        actions: list[dict] = []
        done: list[str] = []
        for r in rows:
            day = str(r.get("day") or "")
            mtype = str(r.get("meeting_type") or "meeting")
            summary = str(r.get("summary") or "").strip()
            if summary:
                summaries.append(summary)
                done.append(f"{day} [{mtype}] {summary[:160]}")
            decisions += [
                str(d)[:160] for d in json.loads(r.get("decisions_json") or "[]") if d
            ]
            risks += [
                str(x)[:160] for x in json.loads(r.get("risks_json") or "[]") if x
            ]
            for a in json.loads(r.get("actions_json") or "[]"):
                if isinstance(a, dict) and str(a.get("item") or "").strip():
                    actions.append(a)

        # Upcoming = open actions with deadlines, most immediate first; the
        # Asana snapshot (when the org connected it) adds live board state.
        upcoming = [
            f"{a['item'][:120]}"
            + (f" — {a.get('owner')}" if a.get("owner") else "")
            + (f", due {a.get('deadline')}" if a.get("deadline") else "")
            for a in actions
        ][:10]
        asana_note = ""
        try:
            from .. import asana_client

            if asana_client.connected(org_id):
                asana_note = str(asana_client.workspace_brief(org_id) or "")[:600]
        except Exception:  # noqa: BLE001 — the board note is optional
            asana_note = ""

        rag = _rag(risks, actions, summaries)
        exec_summary = (
            f"{len(rows)} meeting{'s' if len(rows) != 1 else ''} in the last "
            f"7 days; {len(decisions)} decision"
            f"{'s' if len(decisions) != 1 else ''} recorded, "
            f"{len(actions)} open action item"
            f"{'s' if len(actions) != 1 else ''}"
            + (f", {len(risks)} open risk{'s' if len(risks) != 1 else ''}"
               if risks else "")
            + f". Overall status: {rag.upper()} (heuristic estimate)."
        )
        report = {
            "rag": rag,
            "exec_summary": exec_summary,
            "done": done[:10],
            "upcoming": upcoming,
            "risks": risks[:10],
            "decisions_needed": decisions[:10],
            "actions_open": [
                {"item": str(a.get("item") or "")[:160],
                 "owner": str(a.get("owner") or "")[:80],
                 "deadline": str(a.get("deadline") or "")[:80]}
                for a in actions
            ][:15],
        }
        if asana_note:
            report["asana_snapshot"] = asana_note
        return {"ok": True, "report": report, "source_meetings": len(rows)}
    except Exception as e:  # noqa: BLE001 — soft-fail, never a raw stack
        return {"ok": False, "error": f"status report failed ({type(e).__name__})"}
