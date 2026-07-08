"""Watch Laura's Gmail for Google Meet invitations and auto-join them.

Product flow this enables (no extension, no calendar scheduling):
  In a live meeting you click Meet's native "Add people" and add
  laura.ai.122222@gmail.com  ->  Google emails Laura's inbox the meeting link
  ->  this watcher sees the email, extracts the meet.google.com URL
  ->  the backend sends a Recall bot into that exact meeting.

Native "Add people" does NOT create a calendar event, so Recall's calendar sync
can't catch it — the reliable signal is the invitation email in Laura's inbox.

Auth: reuses the same Google OAuth already set up for the calendar. The watcher
needs the `gmail.readonly` scope on that consent. The refresh token comes from
GOOGLE_REFRESH_TOKEN if set, otherwise it is read back from the connected Recall
calendar (which stores it durably) so no extra storage is needed.
"""
from __future__ import annotations

import base64
import re
import time

import httpx

from .config import settings

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Standard Google Meet code: xxx-xxxx-xxx (lowercase letters).
_MEET_RE = re.compile(r"https://meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})")

_client = httpx.Client(timeout=30.0)


def refresh_token() -> str:
    """The Google OAuth refresh token to act as Laura's inbox.

    Prefer the explicit env var (durable across redeploys); otherwise read it back
    from the connected Recall calendar, which stores it for us.
    """
    if settings.google_refresh_token.strip():
        return settings.google_refresh_token.strip()
    return _refresh_token_from_recall()


def _refresh_token_from_recall() -> str:
    try:
        base = settings.recall_api_base.rstrip("/")
        r = _client.get(
            f"{base}/api/v2/calendars/",
            headers={"Authorization": f"Token {settings.recall_api_key}"},
        )
        r.raise_for_status()
        for cal in r.json().get("results", []):
            if cal.get("status") == "connected" and cal.get("oauth_refresh_token"):
                return cal["oauth_refresh_token"]
    except Exception:
        pass
    return ""


def access_token(rt: str) -> str:
    """Exchange the refresh token for a short-lived Gmail access token."""
    r = _client.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": settings.google_calendar_client_id,
            "client_secret": settings.google_calendar_client_secret,
            "refresh_token": rt,
            "grant_type": "refresh_token",
        },
    )
    r.raise_for_status()
    return r.json().get("access_token", "")


def _extract_meet_urls(text: str) -> set[str]:
    return {f"https://meet.google.com/{code}" for code in _MEET_RE.findall(text or "")}


# Address-bearing headers: To/Cc name the invited alias; Delivered-To keeps the
# plus-tag even when a forward rewrites To. Enough to resolve which avatar an
# invite addressed (see avatars.from_invite_email).
_ADDRESS_HEADERS = ("to", "cc", "delivered-to", "x-original-to")
_ADDR_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def _recipient_addresses(msg: dict) -> set[str]:
    out: set[str] = set()
    for h in (msg.get("payload") or {}).get("headers", []) or []:
        if str(h.get("name", "")).lower() in _ADDRESS_HEADERS:
            out.update(a.lower() for a in _ADDR_RE.findall(h.get("value") or ""))
    return out


def _message_meet_urls(token: str, msg_id: str) -> tuple[set[str], set[str]]:
    """Fetch one message: (meet urls in snippet+body, recipient addresses)."""
    r = _client.get(
        f"{GMAIL_API}/messages/{msg_id}",
        params={"format": "full"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    msg = r.json()
    urls = _extract_meet_urls(msg.get("snippet", ""))

    def walk(part: dict) -> None:
        body = (part.get("body") or {}).get("data")
        if body:
            try:
                decoded = base64.urlsafe_b64decode(body + "===").decode(
                    "utf-8", "ignore"
                )
                urls.update(_extract_meet_urls(decoded))
            except Exception:
                pass
        for child in part.get("parts", []) or []:
            walk(child)

    walk(msg.get("payload") or {})
    return urls, _recipient_addresses(msg)


def poll_new_invites(token: str, seen_ids: set[str]) -> list[tuple[str, str, set[str]]]:
    """Return [(message_id, meet_url, recipient_addresses)] for unseen invites.

    Only looks at very recent mail so we react to a live "Add people" invite, not
    stale ones. `seen_ids` is mutated to record everything we've processed. The
    recipient addresses let the caller route a plus-tagged alias (an avatar's
    email) to its avatar.
    """
    r = _client.get(
        f"{GMAIL_API}/messages",
        params={"q": "newer_than:1h meet.google.com", "maxResults": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    out: list[tuple[str, str, set[str]]] = []
    for m in r.json().get("messages", []) or []:
        mid = m.get("id")
        if not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)
        try:
            urls, addrs = _message_meet_urls(token, mid)
            for url in urls:
                out.append((mid, url, addrs))
        except Exception:
            pass
    return out
