"""Meeting → browser bridge (Sable B2/B3, trigger side).

Turns an in-meeting "open <site> and show it" into a live browser view on the
avatar's meeting tile: create an operator session (bound to the org's saved
login when one exists), navigate, and mint the read-only live-view URL the
`talk.html` browser_view overlay renders. All operator work is synchronous DB
+ provider I/O, so callers run these in a threadpool off the transcript→speak
hot path; the delivery (speak/control) stays in main.py where the queue lives.

DELIBERATELY INERT unless BOTH the browser operator and the meeting trigger
flag are on. Nothing here ever raises into the caller — a browser hiccup must
never touch a live meeting.

No transcript text crosses to the operator or the provider: the goal is a
bounded server-built string keyed off a coarse site label, never the raw
utterance (Sable non-negotiable — transcripts are PII).
"""
from __future__ import annotations

from typing import Optional

from .config import settings

# Coarse site label → (start URL when a saved login exists, public fallback URL,
# spoken name). The label is the browser-identity label AND the narration word;
# it is derived by the intent detector from a fixed allowlist, never free text.
_SITES = {
    "asana": ("https://app.asana.com/", "https://help.asana.com/", "Asana"),
}

# Active browser view per meeting (keyed by meeting_ref = bot_id) so a later
# dismiss / meeting-end can close exactly the right operator session. Bounded:
# one entry per live meeting, cleared on close.
_ACTIVE: dict[str, tuple[str, str]] = {}  # meeting_ref -> (org_id, session_id)


# Per-site, per-task BOUNDED goal strings (≤300 chars, server-built from the
# canonical task key — never the utterance). Each is read-only by construction:
# "point out … do not change anything", so the walkthrough shows WHERE without
# performing the write (the read-only posture enforces this regardless).
_TASK_GOALS = {
    "asana": {
        "create_task": "Read-only demo: show how to create a task in Asana. "
                       "Open a project and point out the Add-task control. Do "
                       "not create anything.",
        "change_assignee": "Read-only demo: show how to change a task's "
                           "assignee in Asana. Open a task and point out the "
                           "Assignee field. Do not change anything.",
        "set_due_date": "Read-only demo: show how to set a task's due date in "
                        "Asana. Open a task and point out the Due-date field. "
                        "Do not change anything.",
        "create_project": "Read-only demo: show how to start a new project in "
                          "Asana. Point out the New-project control. Do not "
                          "create anything.",
        "add_section": "Read-only demo: show how to add a section to a project "
                       "in Asana. Open a project and point out where sections "
                       "are added. Do not change anything.",
        "add_comment": "Read-only demo: show how to comment on a task in "
                       "Asana. Open a task and point out the comment box. Do "
                       "not post anything.",
        "tour": "Read-only tour of the Asana workspace: show the sidebar, a "
                "project, and a task. Do not change anything.",
    },
}

# Operation → narration. When the target control has a short label (from the
# sanitized observation — bounded + redacted upstream), we name it so the
# narration tracks what the pointer is doing ("Now I'll click Add task"); with
# no usable label we fall back to a generic line. UI-control labels on the
# user's own workspace, shown on their own tile, are safe to voice; secrets are
# redacted and length is capped upstream.
_STEP_NAMED = {
    "click": ["Now I'll click {name}.", "Let's open {name}.",
              "Here — I'll select {name}."],
    "type": ["This is where you'd fill in {name}.",
             "You'd type into {name} here."],
    "navigate": ["Opening {name}.", "Heading to {name}."],
}
_STEP_LINES = {
    "navigate": ["Let me pull that up.", "Opening that view.",
                 "Heading there now."],
    "click": ["I'll open this here.", "Selecting that.",
              "Let's go in here."],
    "scroll": ["Let me scroll down to find it.", "Scrolling down."],
    "type": ["This is where you'd type it in.",
             "Here's the field you'd fill in."],
    "read": ["Let me take a look here.", "Reading this."],
}
_STEP_DEFAULT = ["Next, this part.", "And over here."]

_CLOSING = {
    "cancelled": "",
    "finished": "That's the flow — that's how you'd do it.",
    "awaiting_approval": "That's the point where you'd confirm it — I'll leave "
                         "the actual change to you.",
    "write_rejected_read_only": "That's where you'd make the change — I only "
                                "show the steps, I don't change anything.",
    "budget_exhausted": "I'll stop there — that's the gist of it.",
    "stalled": "I'll stop there — that's the main idea.",
    "expired": "The view timed out, but that's the path.",
    "blocked": "I can't go further there, but that's the path.",
    "disabled": "",
    "error": "",
}


def _narration_for(info, index: int) -> str:
    """Build a step line from the info dict {operation, role, name}. Prefers a
    named line when a usable control label exists, else a generic line."""
    if isinstance(info, str):  # back-compat: bare operation
        info = {"operation": info, "name": ""}
    op = info.get("operation", "")
    name = (info.get("name") or "").strip()
    if name and op in _STEP_NAMED:
        lines = _STEP_NAMED[op]
        return lines[index % len(lines)].format(name=name)
    lines = _STEP_LINES.get(op, _STEP_DEFAULT)
    return lines[index % len(lines)]


def run_walkthrough(org_id: str, session_id: str, *, site_label: str,
                    task_key: str, on_narrate, cancel=None) -> dict:
    """Drive the visual planner through a read-only how-to on an ALREADY-open,
    presented session, narrating each step via ``on_narrate`` (a thread-safe
    callback the caller supplies). Returns {ok, outcome, closing}. Sync
    (threadpool). Never raises — a walkthrough fault must not touch the meeting.

    ``on_narrate(line: str)`` is called once per action, BEFORE it happens, so
    the voice leads the on-screen click. The line is built from the operation
    only — never from page content."""
    goal = (_TASK_GOALS.get(site_label, {}) or {}).get(task_key, "")
    if not goal:
        return {"ok": False, "outcome": "unknown_task", "closing": ""}
    try:
        from .browser import coordinator

        def _on_step(index: int, operation: str, info) -> None:
            try:
                on_narrate(_narration_for(info, index))
            except Exception:  # noqa: BLE001 — narration never breaks the run
                pass

        result = coordinator.run(org_id, session_id, goal,
                                 principal="meeting", on_step=_on_step,
                                 cancel=cancel)
        outcome = str(result.get("outcome") or "error")
        return {"ok": outcome in ("finished", "awaiting_approval",
                                  "write_rejected_read_only"),
                "outcome": outcome,
                "closing": _CLOSING.get(outcome, "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "outcome": type(exc).__name__, "closing": ""}


def trigger_enabled() -> bool:
    """Both switches: the operator must be on AND the meeting trigger opted in.
    Off by default — with either off this whole module no-ops."""
    from . import browser

    return bool(settings.browser_meeting_trigger_enabled) and browser.enabled()


def site_spoken_name(site_label: str) -> str:
    entry = _SITES.get(site_label)
    return entry[2] if entry else site_label


def open_for_meeting(org_id: str, *, avatar_key: str, site_label: str,
                     meeting_ref: str) -> dict:
    """Create + navigate + present a browser view for a meeting. Returns
    {ok, url, spoken, logged_in} on success or {ok: False, reason, spoken}.
    Sync (threadpool). Never raises."""
    entry = _SITES.get(site_label)
    if entry is None:
        return {"ok": False, "reason": "unknown_site", "spoken": ""}
    app_url, public_url, spoken = entry
    try:
        from .browser import dal, operator

        ident = dal.identity_internal(org_id, site_label)
        logged_in = ident is not None
        target = app_url if logged_in else public_url
        # Close any prior view for this meeting first (one tile at a time).
        _close_existing(org_id, meeting_ref)

        session = operator.create_session(
            org_id, principal="meeting", avatar_key=avatar_key,
            meeting_ref=meeting_ref,
            identity_label=site_label if logged_in else "",
            metadata={"purpose": "meeting_browse", "site": site_label},
        )
        sid = session["id"]
        operator.issue_command(org_id, sid, verb="navigate", url=target,
                               principal="meeting")
        minted = operator.present(org_id, sid, principal="meeting")
        if not minted.get("ok"):
            operator.close_session(org_id, sid, principal="meeting")
            return {"ok": False, "reason": "present_failed", "spoken": spoken}
        viewer = operator.exchange_token(org_id, minted["presentation_token"])
        url = ((viewer or {}).get("viewer") or {}).get("url", "")
        if not url:
            operator.close_session(org_id, sid, principal="meeting")
            return {"ok": False, "reason": "no_viewer_url", "spoken": spoken}
        _ACTIVE[meeting_ref] = (org_id, sid)
        return {"ok": True, "url": url, "spoken": spoken,
                "logged_in": logged_in, "session_id": sid}
    except Exception as exc:  # noqa: BLE001 — a browse fault never hits the meeting
        # Class name only; never a payload/URL/credential.
        return {"ok": False, "reason": type(exc).__name__, "spoken": spoken}


def close_for_meeting(meeting_ref: str) -> bool:
    """Close the meeting's active browser view, if any. Sync, never raises."""
    active = _ACTIVE.pop(meeting_ref, None)
    if active is None:
        return False
    org_id, sid = active
    try:
        from .browser import operator

        operator.close_session(org_id, sid, principal="meeting")
    except Exception:  # noqa: BLE001 — best-effort; TTL/reconcile is the backstop
        return False
    return True


def has_active(meeting_ref: str) -> bool:
    return meeting_ref in _ACTIVE


def _close_existing(org_id: str, meeting_ref: str) -> None:
    active = _ACTIVE.pop(meeting_ref, None)
    if active is None:
        return
    _prev_org, sid = active
    try:
        from .browser import operator

        operator.close_session(_prev_org, sid, principal="meeting")
    except Exception:  # noqa: BLE001
        pass
