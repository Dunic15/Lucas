"""Google actions the avatar performs ITSELF (autonomous execution).

This is the "AI employee that does the work" path: at meeting end an avatar
— from the connected Google account (the same OAuth calendar/Gmail/Drive
already use) — sends the recap email and files a meeting-notes doc in its Drive
folder. It complements, and is independent of, the Cedric-in-Slack approval
path (an orchestrator can still own delivery). `create_calendar_event` is
shipped for a future follow-up-booking flow but is not yet wired into finalize
(and its calendar.events scope is intentionally not requested until it is).

Discipline (identical to the rest of the connectors):
- NEVER on the live path — only called at finalize, in a threadpool.
- Best-effort — every function returns a status dict and never raises into the
  caller; a vendor failure can't block the meter stop.
- Distilled only — subject/body/notes are built from the artifact
  (summary/actions/decisions), never from raw transcript lines.
- Scopes: sending mail needs gmail.send, creating events needs
  calendar.events, writing Drive needs drive.file — all added to
  /oauth/google/connect, so a one-time reconnect grants them.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from typing import Any, Optional

import httpx

from . import gmail_watcher

GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
CALENDAR_EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
DRIVE_FILES = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"

_client = httpx.Client(timeout=15.0)


def _token() -> str:
    """Access token for the connected Google account (reuses the calendar/Gmail
    refresh token). '' when no account is connected."""
    rt = gmail_watcher.refresh_token()
    return gmail_watcher.access_token(rt) if rt else ""


_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def parse_deadline(text: str, ref: Optional[date] = None) -> Optional[date]:
    """Best-effort, CONSERVATIVE deadline → date. Returns None (skip) unless the
    date is unambiguous — a wrong calendar date is worse than none. Handles:
    ISO YYYY-MM-DD, today/tomorrow, a weekday name (next occurrence), next week.
    Anything vaguer ('soon', 'end of quarter', 'Q3') → None."""
    if not text:
        return None
    ref = ref or date.today()
    t = text.strip().lower()

    m = _ISO_RE.search(t)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d if d >= ref else None  # never book a past deadline
        except ValueError:
            return None
    if "today" in t:
        return ref
    if "tomorrow" in t:
        return ref + timedelta(days=1)
    if "next week" in t:
        return ref + timedelta(days=7)
    for word, wd in _WEEKDAYS.items():
        if re.search(rf"\b{word}\b", t):
            ahead = (wd - ref.weekday()) % 7  # 0 = the same weekday → today
            return ref + timedelta(days=ahead)
    return None


def send_gmail(to: list[str], subject: str, body: str) -> dict[str, Any]:
    """Send an email as the connected account via the Gmail API. Best-effort."""
    to = [e for e in (to or []) if e]
    if not to:
        return {"sent": False, "reason": "no recipients"}
    try:
        token = _token()
        if not token:
            return {"sent": False, "reason": "no google account connected"}
        msg = MIMEText(body or "", "plain", "utf-8")
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject or "Meeting follow-up"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = _client.post(
            GMAIL_SEND,
            headers={"Authorization": f"Bearer {token}"},
            json={"raw": raw},
        )
        ok = 200 <= r.status_code < 300
        return {"sent": ok, "status_code": r.status_code,
                "error": None if ok else r.text[:200]}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "reason": type(e).__name__}


def create_calendar_event(
    summary: str, start_iso: str, end_iso: str,
    attendees: list[str] | None = None, tz: str = "",
) -> dict[str, Any]:
    """Create a Calendar event on the primary calendar. start/end are naive
    RFC3339 (no offset); `tz` (IANA, e.g. 'Europe/Rome') is required by Google
    when the dateTime has no offset — falls back to settings.execute_timezone.
    Best-effort; needs the calendar.events (write) scope."""
    if not (summary and start_iso and end_iso):
        return {"created": False, "reason": "missing summary/start/end"}
    try:
        from .config import settings as _s
        zone = tz or _s.execute_timezone or "UTC"
        token = _token()
        if not token:
            return {"created": False, "reason": "no google account connected"}
        body: dict[str, Any] = {
            "summary": summary[:200],
            "start": {"dateTime": start_iso, "timeZone": zone},
            "end": {"dateTime": end_iso, "timeZone": zone},
        }
        emails = [e for e in (attendees or []) if e and "@" in e]
        if emails:
            body["attendees"] = [{"email": e} for e in emails]
        r = _client.post(
            CALENDAR_EVENTS,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        ok = 200 <= r.status_code < 300
        return {"created": ok, "status_code": r.status_code,
                "event_id": (r.json().get("id") if ok else None),
                "error": None if ok else r.text[:200]}
    except Exception as e:  # noqa: BLE001
        return {"created": False, "reason": type(e).__name__}


def write_drive_note(folder_id: str, title: str, content: str) -> dict[str, Any]:
    """Create a plain-text file (converted to a Google Doc) in a Drive folder.
    Best-effort; needs the drive.file scope. '' folder → skip."""
    folder_id = (folder_id or "").strip()
    if not folder_id:
        return {"written": False, "reason": "no folder configured"}
    try:
        token = _token()
        if not token:
            return {"written": False, "reason": "no google account connected"}
        meta = {
            "name": title[:200] or "Meeting notes",
            "parents": [folder_id],
            "mimeType": "application/vnd.google-apps.document",
        }
        # multipart/related: JSON metadata part + the text body part. A random
        # boundary can't collide with anything in the content.
        boundary = f"laura-{uuid.uuid4().hex}"
        parts = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps(meta)
            + f"\r\n--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n"
            + (content or "")
            + f"\r\n--{boundary}--"
        )
        r = _client.post(
            DRIVE_FILES,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            content=parts.encode("utf-8"),
        )
        ok = 200 <= r.status_code < 300
        return {"written": ok, "status_code": r.status_code,
                "file_id": (r.json().get("id") if ok else None),
                "error": None if ok else r.text[:200]}
    except Exception as e:  # noqa: BLE001
        return {"written": False, "reason": type(e).__name__}
