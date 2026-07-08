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
import uuid
from email.mime.text import MIMEText
from typing import Any

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
    summary: str, start_iso: str, end_iso: str, attendees: list[str] | None = None
) -> dict[str, Any]:
    """Create a Calendar event on the primary calendar. start/end are RFC3339.
    Best-effort; needs the calendar.events (write) scope."""
    if not (summary and start_iso and end_iso):
        return {"created": False, "reason": "missing summary/start/end"}
    try:
        token = _token()
        if not token:
            return {"created": False, "reason": "no google account connected"}
        body: dict[str, Any] = {
            "summary": summary[:200],
            "start": {"dateTime": start_iso},
            "end": {"dateTime": end_iso},
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
