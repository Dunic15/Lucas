"""Autopilot follow-ups: Laura acts between meetings.

Three behaviors, each behind its own env flag and ALL OFF by default (the
zero-key demo and the meter are untouched unless the owner opts in):

  AUTOPILOT_DELIVER=true  — when a meeting finalizes, send the drafted
                            follow-up email to AUTOPILOT_DELIVER_TO and post
                            the artifact summary to Slack. No more manual
                            /deliver call.
  AUTOPILOT_BRIEF=true    — when a session starts on a meeting link that has
                            ledger history, send the carryover brief ("what's
                            still open from last time") to AUTOPILOT_BRIEF_TO
                            (falls back to AUTOPILOT_DELIVER_TO) + Slack.
  AUTOPILOT_NUDGE=true    — every AUTOPILOT_NUDGE_HOURS, post a Slack digest
                            of every open ledger item across meetings, so
                            owners get chased without anyone asking.

Failure discipline: autopilot is best-effort — a vendor error must never
block finalize (the meter stop) or a meeting join, so every entry point
swallows exceptions and reports status dicts instead. Content is never
logged; these functions only hand distilled text to actions.send_email /
post_to_slack, which the operator explicitly configured.
"""
from __future__ import annotations

import time
from typing import Any

from . import actions, google_actions, ledger
from .config import settings


def _recipients(raw: str) -> list[str]:
    return [e.strip() for e in (raw or "").split(",") if e.strip()]


# ─────────── finalize: autonomous EXECUTION (the AI-employee path) ───────────
# When EXECUTE_ENABLED is on, an avatar does the post-meeting work ITSELF from
# the connected Google account: emails the recap, files a notes doc in its
# Drive folder. Independent of the orchestrator/Slack path and of SendGrid —
# it uses the Gmail/Drive OAuth already connected. All best-effort, off the
# live path (called from _finalize_session in a background task).

def _recap_email(avatar_name: str, artifact: dict[str, Any]) -> tuple[str, str]:
    """Compose (subject, body) for the recap from the distilled artifact —
    prefers the model-drafted follow_up_email, else builds one from the
    summary + actions + decisions. Never touches the transcript."""
    email = artifact.get("follow_up_email") or {}
    if email.get("subject") and email.get("body"):
        return email["subject"], email["body"] + f"\n\n— {avatar_name}"
    lines = [artifact.get("summary", "").strip(), ""]
    decisions = artifact.get("decisions") or []
    if decisions:
        lines.append("Decisions:")
        lines += [f"  • {d}" for d in decisions]
        lines.append("")
    actions_list = artifact.get("actions") or []
    if actions_list:
        lines.append("Action items:")
        for a in actions_list:
            if isinstance(a, dict):
                who = a.get("owner") or "unassigned"
                due = f" — due {a['deadline']}" if a.get("deadline") else ""
                lines.append(f"  • {a.get('item','')} ({who}){due}")
            else:
                lines.append(f"  • {a}")
        lines.append("")
    lines.append(f"— {avatar_name}")
    return "Meeting recap & next steps", "\n".join(lines).strip()


def _notes_doc(avatar_name: str, artifact: dict[str, Any]) -> tuple[str, str]:
    """A meeting-notes document title + markdown body for the Drive folder."""
    mtype = (artifact.get("meeting_type") or "Meeting").replace("_", " ").title()
    title = f"{mtype} notes — {avatar_name}"
    subj, body = _recap_email(avatar_name, artifact)
    risks = artifact.get("risks") or []
    extra = ("\n\nRisks / watch-outs:\n" + "\n".join(f"  • {r}" for r in risks)) if risks else ""
    return title, f"# {title}\n\n{body}{extra}"


def _artifact_is_empty(artifact: dict[str, Any]) -> bool:
    return not (
        (artifact.get("summary") or "").strip()
        or artifact.get("decisions")
        or artifact.get("actions")
    )


def maybe_execute(
    avatar_name: str, artifact: dict[str, Any], drive_folder_id: str = "",
    attendee_emails: list[str] | None = None,
) -> dict[str, Any]:
    """Do the post-meeting work autonomously if EXECUTE_ENABLED. Returns a
    status dict; never raises. Recap → EXPLICIT recipients only
    (EXECUTE_RECAP_TO); notes → the avatar's Drive folder.

    Recipient safety: the recap contains decisions/pricing/etc, so it is sent
    ONLY to the operator-configured EXECUTE_RECAP_TO — never silently to
    orchestrator-supplied meeting attendees (who may be external). A caller
    that wants attendee delivery must put those addresses in EXECUTE_RECAP_TO
    (or approve them via the orchestrator/Slack path)."""
    if not settings.execute_enabled:
        return {"executed": False, "reason": "disabled"}
    if _artifact_is_empty(artifact):
        return {"executed": False, "reason": "empty artifact"}
    out: dict[str, Any] = {"executed": True}
    try:
        if settings.execute_recap_email:
            to = _recipients(settings.execute_recap_to)
            if to:
                subject, body = _recap_email(avatar_name, artifact)
                out["email"] = google_actions.send_gmail(to, subject, body)
            else:
                # No configured recipients → do NOT fall back to (possibly
                # external) meeting attendees. Explicit allowlist only.
                out["email"] = {"sent": False, "reason": "EXECUTE_RECAP_TO not set"}
        if settings.execute_drive_notes and drive_folder_id:
            title, content = _notes_doc(avatar_name, artifact)
            out["drive"] = google_actions.write_drive_note(drive_folder_id, title, content)
        if settings.execute_calendar:
            out["calendar"] = _book_deadlines(avatar_name, artifact)
        if settings.execute_slack:
            out["slack"] = actions.post_to_slack(_slack_recap(avatar_name, artifact))
    except Exception as e:  # never block finalize
        return {"executed": False, "reason": type(e).__name__}
    return out


def _slack_recap(avatar_name: str, artifact: dict[str, Any]) -> str:
    """A Slack-mrkdwn recap: summary, decisions, and action items — with the
    ones asked out loud in the meeting flagged (they're the approval-worthy
    'please do X' asks). Distilled data only."""
    lines = [f"*{avatar_name} — meeting recap*"]
    if artifact.get("summary"):
        lines += [artifact["summary"], ""]
    decisions = artifact.get("decisions") or []
    if decisions:
        lines.append("*Decisions*")
        lines += [f"• {d}" for d in decisions]
        lines.append("")
    actions_list = artifact.get("actions") or []
    if actions_list:
        lines.append("*Action items*")
        for a in actions_list:
            if isinstance(a, dict):
                who = a.get("owner") or "unassigned"
                due = f" — due {a['deadline']}" if a.get("deadline") else ""
                flag = "  :speech_balloon: _asked in meeting_" if a.get("requested_live") else ""
                lines.append(f"• {a.get('item','')} ({who}){due}{flag}")
            else:
                lines.append(f"• {a}")
    return "\n".join(lines).strip()


# Cap the number of events one meeting can create — never spam the calendar.
_MAX_CAL_EVENTS = 6


def _book_deadlines(avatar_name: str, artifact: dict[str, Any]) -> dict[str, Any]:
    """Put each action's parseable deadline on the calendar as a 30-min reminder
    block (09:00 local on the due date). Only UNAMBIGUOUS dates are booked —
    parse_deadline returns None for anything vague, so nothing lands on a guessed
    date. Best-effort per event."""
    booked, skipped = 0, 0
    for a in (artifact.get("actions") or [])[:20]:
        if not isinstance(a, dict):
            continue
        due = google_actions.parse_deadline(a.get("deadline", ""))
        item = (a.get("item") or "").strip()
        if not due or not item:
            skipped += 1
            continue
        if booked >= _MAX_CAL_EVENTS:
            break
        start = f"{due.isoformat()}T09:00:00"
        end = f"{due.isoformat()}T09:30:00"
        owner = a.get("owner") or ""
        summary = f"[{avatar_name}] {item}"
        res = google_actions.create_calendar_event(
            summary, start, end, attendees=None,
        )
        booked += 1 if res.get("created") else 0
        skipped += 0 if res.get("created") else 1
    return {"booked": booked, "skipped": skipped}


# ─────────────────────── finalize: auto-deliver ───────────────────────
def maybe_deliver(avatar_name: str, artifact: dict[str, Any]) -> dict[str, Any]:
    """Send the finished artifact (email + Slack) if AUTOPILOT_DELIVER is on."""
    if not settings.autopilot_deliver:
        return {"delivered": False, "reason": "disabled"}
    try:
        email = artifact.get("follow_up_email") or {}
        to = _recipients(settings.autopilot_deliver_to)
        email_res: dict[str, Any] = {"sent": False, "reason": "no recipients"}
        if to and email.get("subject") and email.get("body"):
            email_res = actions.send_email(to, email["subject"], email["body"])
        slack_res = actions.post_to_slack(
            actions.artifact_to_slack_text(avatar_name, artifact)
        )
        return {"delivered": True, "email": email_res, "slack": slack_res}
    except Exception as e:  # never block finalize on a vendor hiccup
        return {"delivered": False, "reason": type(e).__name__}


# ────────────────────── session start: brief ──────────────────────────
def maybe_send_brief(meeting_url: str, avatar_name: str) -> dict[str, Any]:
    """Mail/Slack the carryover brief when a known meeting starts, if enabled."""
    if not settings.autopilot_brief:
        return {"sent": False, "reason": "disabled"}
    try:
        brief = ledger.carryover_brief(meeting_url)
        if not brief:
            return {"sent": False, "reason": "no history"}
        key = ledger.meeting_key(meeting_url)
        subject = f"{avatar_name} — pre-meeting brief ({key})"
        body = (
            f"Your meeting is starting. Here's what previous sessions left open:\n\n"
            f"{brief}\n\n— {avatar_name}"
        )
        to = _recipients(settings.autopilot_brief_to or settings.autopilot_deliver_to)
        email_res: dict[str, Any] = {"sent": False, "reason": "no recipients"}
        if to:
            email_res = actions.send_email(to, subject, body)
        slack_res = actions.post_to_slack(f"*{subject}*\n{brief}")
        return {"sent": True, "email": email_res, "slack": slack_res}
    except Exception as e:  # never block a meeting join
        return {"sent": False, "reason": type(e).__name__}


# ───────────────────────── periodic nudges ────────────────────────────
def nudge_digest() -> str:
    """One Slack-ready digest of every open ledger item, grouped by meeting.
    Empty string when nothing is open (callers skip posting)."""
    by_meeting = ledger.open_by_meeting()
    if not by_meeting:
        return ""
    lines = ["*Open items Laura is tracking across meetings:*"]
    for key, items in sorted(by_meeting.items()):
        lines.append(f"\n*{key}* — {len(items)} open")
        for it in items[:10]:
            label = (
                ledger.humanize_step(it["item"])
                if it["kind"] == "missing_step"
                else it["item"]
            )
            owner = f" (owner: {it['owner']})" if it.get("owner") else ""
            due = f" (due {it['deadline']})" if it.get("deadline") else ""
            kind = "process step" if it["kind"] == "missing_step" else "action"
            lines.append(f"• [{kind}] {label}{owner}{due}")
    return "\n".join(lines)


_last_nudge = {"at": 0.0}


def nudge_due(now: float | None = None) -> bool:
    if not settings.autopilot_nudge:
        return False
    now = time.time() if now is None else now
    return (now - _last_nudge["at"]) >= settings.autopilot_nudge_hours * 3600


def run_nudge() -> dict[str, Any]:
    """Post the digest to Slack (called from the periodic loop in main)."""
    try:
        digest = nudge_digest()
        _last_nudge["at"] = time.time()
        if not digest:
            return {"sent": False, "reason": "nothing open"}
        return actions.post_to_slack(digest)
    except Exception as e:
        return {"sent": False, "reason": type(e).__name__}
