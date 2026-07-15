"""Native Google execution — Calendar events.insert + Gmail messages.send.

The write half of the native executor (docs/product/NATIVE-INTEGRATIONS-PLAN.md,
"Now" slice): given an org that connected its Google account via
``/oauth/google/connect``, mint a short-lived access token from the stored
per-org refresh token (``store.get_org_oauth``) and make ONE Google API call.

Contract for every entry point:
- Takes ``org_id`` + a plain dict of already-distilled fields (never transcript
  content). Resolves the token by org — a wrong/absent org yields "not
  connected", never a call from the wrong account.
- Returns ``{"ok": True, ...provenance...}`` or ``{"ok": False, "error": str}``.
  It NEVER raises: this runs at finalize/approval, off the live-meeting path,
  and a Google hiccup must degrade to a soft "failed" receipt, not a 500.
- Logs no token and no transcript.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

import httpx

from . import store
from .config import settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Same collection endpoint serves events.insert (POST) and events.list (GET).
_CAL_INSERT = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_CAL_LIST = _CAL_INSERT
_GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_TIMEOUT = 30.0


def _access_token(org_id: str) -> tuple[str, str]:
    """(access_token, "") for the org, or ("", error) when unavailable."""
    if not settings.google_calendar_client_id or not settings.google_calendar_client_secret:
        return "", "Google OAuth client is not configured"
    oauth = store.get_org_oauth(org_id)
    if not oauth or not oauth.get("refresh_token"):
        return "", "Google is not connected for this org"
    try:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "client_id": settings.google_calendar_client_id,
                "client_secret": settings.google_calendar_client_secret,
                "refresh_token": oauth["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return "", f"token request failed ({type(e).__name__})"
    if resp.status_code >= 300:
        return "", f"token refresh rejected (HTTP {resp.status_code})"
    tok = resp.json().get("access_token", "")
    return (tok, "") if tok else ("", "no access token returned")


def _emails(value: Any) -> list[str]:
    if isinstance(value, str):
        return [e.strip() for e in value.replace(";", ",").split(",") if e.strip()]
    if isinstance(value, (list, tuple)):
        return [str(e).strip() for e in value if str(e).strip()]
    return []


def create_calendar_event(org_id: str, event: dict) -> dict:
    """Create a Calendar event on the org's primary calendar (events.insert).

    ``event``: {title/summary, start (RFC3339), end (RFC3339), attendees?,
    description?, timezone?}. Attendees are invited (sendUpdates=all)."""
    summary = str(event.get("title") or event.get("summary") or "").strip()
    start = str(event.get("start") or "").strip()
    end = str(event.get("end") or "").strip()
    if not summary or not start or not end:
        return {"ok": False, "error": "event needs title, start and end"}
    tz = str(event.get("timezone") or event.get("time_zone") or "UTC").strip() or "UTC"

    token, err = _access_token(org_id)
    if err:
        return {"ok": False, "error": err}

    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
        # Provision a real Google Meet conference for the event so the scheduled
        # meeting actually has a join link (not just a bare calendar hold). The
        # avatar + humans join THIS Meet. requestId must be unique per create
        # call; mint it the same way the ledger mints action ids (uuid4 hex).
        # This runs at scheduling / finalize time — off the live-meeting hot
        # path — so a uuid here is fine (the hot-path no-uuid rule is n/a here).
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    if event.get("description"):
        body["description"] = str(event["description"])
    attendees = _emails(event.get("attendees"))
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]

    try:
        resp = httpx.post(
            _CAL_INSERT,
            # conferenceDataVersion=1 is REQUIRED for Google to honour the
            # createRequest and actually mint the Meet link.
            params={"sendUpdates": "all", "conferenceDataVersion": 1},
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"calendar request failed ({type(e).__name__})"}
    if resp.status_code >= 300:
        return {"ok": False, "error": f"calendar insert failed (HTTP {resp.status_code})"}
    data = resp.json()
    return {
        "ok": True,
        "event_id": data.get("id", ""),
        "event_url": data.get("htmlLink", ""),
        # The Meet join URL: prefer the top-level hangoutLink, else the video
        # entry point Google returns under conferenceData. Empty string when
        # (for any reason) no conference was provisioned — callers guard null.
        "meet_url": _meet_url(data),
    }


def _meet_url(data: dict) -> str:
    """The Google Meet join URL from an events.insert response, or ""."""
    link = str(data.get("hangoutLink") or "").strip()
    if link:
        return link
    conf = data.get("conferenceData") or {}
    entry_points = conf.get("entryPoints") or []
    video = next(
        (e for e in entry_points if e.get("entryPointType") == "video" and e.get("uri")),
        None,
    )
    if video:
        return str(video.get("uri") or "").strip()
    for e in entry_points:
        if e.get("uri"):
            return str(e["uri"]).strip()
    return ""


def list_calendar_events(org_id: str, *, max_results: int = 20) -> dict:
    """List UPCOMING events on the org's primary Google Calendar (read-only).

    Mirrors ``create_calendar_event`` / ``send_gmail``: mint a short-lived access
    token from the org's stored refresh token, then GET events with
    ``timeMin=now``, ``singleEvents=true``, ``orderBy=startTime`` and a small
    ``maxResults``. The ``calendar.events.readonly`` scope is granted at connect.

    Returns ``{"ok": True, "events": [...raw Google items...]}`` (the caller
    distills title / start / attendees / meeting URL) or
    ``{"ok": False, "error": str}``. It NEVER raises: this feeds a dashboard READ
    off the live path, so a Google hiccup must degrade to an empty list, not a
    500. Logs no token and no event content."""
    token, err = _access_token(org_id)
    if err:
        # Diagnostic (status only, no token/email/event content): why a connected
        # org's calendar shows empty — token-refresh rejected, not connected, etc.
        print(f"[calendar] read blocked org={str(org_id)[:10]}: {err}", flush=True)
        return {"ok": False, "error": err}
    try:
        n = int(max_results or 20)
    except (TypeError, ValueError):
        n = 20
    n = max(1, min(n, 50))
    try:
        resp = httpx.get(
            _CAL_LIST,
            params={
                "timeMin": datetime.now(timezone.utc).isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": n,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"calendar list failed ({type(e).__name__})"}
    if resp.status_code >= 300:
        # Diagnostic: a 403 here on a Workspace domain usually = admin/API access
        # restriction on the (unverified) app; 401 = token/scope. No PII logged.
        print(f"[calendar] list org={str(org_id)[:10]} HTTP {resp.status_code}", flush=True)
        return {"ok": False, "error": f"calendar list failed (HTTP {resp.status_code})"}
    try:
        items = resp.json().get("items", [])
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "calendar list returned no JSON"}
    _n = len(items) if isinstance(items, list) else 0
    # Diagnostic: items=0 on a connected org = right token but no events in the
    # window (wrong account/calendar), vs a non-200 above = an API/auth failure.
    print(f"[calendar] list org={str(org_id)[:10]} ok items={_n}", flush=True)
    return {"ok": True, "events": items if isinstance(items, list) else []}


def send_gmail(org_id: str, message: dict) -> dict:
    """Send an email as the org's Google account (Gmail messages.send).

    ``message``: {to (str|list of emails), subject, body}."""
    to = _emails(message.get("to"))
    subject = str(message.get("subject") or "").strip()
    text = str(message.get("body") or message.get("text") or "")
    if not to:
        return {"ok": False, "error": "email needs at least one recipient"}
    if not subject and not text:
        return {"ok": False, "error": "email needs a subject or a body"}

    token, err = _access_token(org_id)
    if err:
        return {"ok": False, "error": err}

    mime = MIMEText(text, _charset="utf-8")
    mime["To"] = ", ".join(to)
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    try:
        resp = httpx.post(
            _GMAIL_SEND,
            headers={"Authorization": f"Bearer {token}"},
            json={"raw": raw},
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"gmail request failed ({type(e).__name__})"}
    if resp.status_code >= 300:
        return {"ok": False, "error": f"gmail send failed (HTTP {resp.status_code})"}
    data = resp.json()
    return {
        "ok": True,
        "message_id": data.get("id", ""),
        "thread_id": data.get("threadId", ""),
    }
