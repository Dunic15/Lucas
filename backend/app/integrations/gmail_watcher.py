"""Watch Laura's Gmail for meeting invitations and auto-join them.

Product flow this enables (no extension, no calendar scheduling):
  Any email that lands in Laura's inbox with a joinable meeting link. Meet's
  native "Add people" (which emails the meet.google.com URL), a forwarded Zoom
  invite, a forwarded Teams invite  ->  this watcher sees the email, extracts
  the meeting URL  ->  the backend sends a Recall bot into that exact meeting.

Meet's "Add people" does NOT create a calendar event, so Recall's calendar sync
can't catch it; the reliable signal is the invitation email in Laura's inbox.
Zoom/Teams links are kept whole (?pwd=, the meetup-join context); stripping
their join credentials would strand the bot at the passcode screen.

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

from ..config import settings

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Standard Google Meet code: xxx-xxxx-xxx (lowercase letters).
_MEET_RE = re.compile(r"https://meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})")
# Zoom join links (any subdomain: zoom.us, us02web.zoom.us, company.zoom.us).
# The whole URL is kept. ?pwd= is the embedded passcode the bot needs to join.
_ZOOM_RE = re.compile(
    r"https://(?:[\w-]+\.)?zoom\.us/(?:j|s|w|wc/join|wc)/\d{8,13}[^\s\"'<>]*"
)
# Teams deep links: classic /l/meetup-join/<thread>/… and new /meet/<id>?p=…
# forms, on both teams.microsoft.com and teams.live.com. Kept whole for the
# same reason (the context/passcode parts are load-bearing).
_TEAMS_RE = re.compile(
    r"https://teams\.(?:microsoft|live)\.com/(?:l/meetup-join|meet)/[^\s\"'<>]+"
)
# Gmail search that matches any of the three platforms' invite emails.
_INVITE_QUERY = (
    "newer_than:1h (meet.google.com OR zoom.us OR teams.microsoft.com OR teams.live.com)"
)

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


def _extract_meeting_urls(text: str) -> set[str]:
    """Every joinable Meet/Zoom/Teams URL in `text`, normalized.

    Meet links are rebuilt from the room code (their query params are tracking
    noise); Zoom/Teams links are kept whole minus trailing punctuation; their
    query carries the passcode/context needed to actually get into the call.
    """
    text = text or ""
    urls = {f"https://meet.google.com/{code}" for code in _MEET_RE.findall(text)}
    for pattern in (_ZOOM_RE, _TEAMS_RE):
        urls.update(u.rstrip(".,;:!)]") for u in pattern.findall(text))
    return urls


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


# NOTE: the sender (From/Reply-To) is deliberately NOT extracted for org
# attribution. Those headers are unauthenticated (Reply-To gets no SPF/DKIM/
# DMARC check) and this watcher reads ONE shared inbox, so a forged header
# could bill and arm an unrelated tenant's org inside an attacker's meeting.
# Gmail-invited meetings stay on the Demo org (see main.py's gmail loop);
# safe per-user attribution waits on a per-user mailbox trust anchor.


def _message_meeting_urls(token: str, msg_id: str) -> tuple[set[str], set[str], float]:
    """Fetch one message: (meeting urls in snippet+body, recipient addresses,
    received-at epoch seconds: 0.0 when Gmail omits internalDate)."""
    r = _client.get(
        f"{GMAIL_API}/messages/{msg_id}",
        params={"format": "full"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    msg = r.json()
    urls = _extract_meeting_urls(msg.get("snippet", ""))

    def walk(part: dict) -> None:
        body = (part.get("body") or {}).get("data")
        if body:
            try:
                decoded = base64.urlsafe_b64decode(body + "===").decode(
                    "utf-8", "ignore"
                )
                urls.update(_extract_meeting_urls(decoded))
            except Exception:
                pass
        for child in part.get("parts", []) or []:
            walk(child)

    walk(msg.get("payload") or {})
    try:
        received_at = float(msg.get("internalDate", 0)) / 1000.0
    except (TypeError, ValueError):
        received_at = 0.0
    return urls, _recipient_addresses(msg), received_at


def poll_new_invites(
    token: str, seen_ids: set[str]
) -> list[tuple[str, str, set[str], float]]:
    """Return [(message_id, meeting_url, recipient_addresses, received_at)] for
    unseen invites: Meet "Add people" invites plus forwarded Zoom/Teams
    invitations.

    Only looks at very recent mail so we react to a live invite, not stale
    ones. `seen_ids` is mutated to record everything we've processed. The
    recipient addresses let the caller route a plus-tagged alias (an avatar's
    email) to its avatar; received_at (epoch seconds) lets the caller's seeding
    pass tell a live invite from stale mail after a restart.
    """
    r = _client.get(
        f"{GMAIL_API}/messages",
        params={"q": _INVITE_QUERY, "maxResults": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    out: list[tuple[str, str, set[str], float]] = []
    for m in r.json().get("messages", []) or []:
        mid = m.get("id")
        if not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)
        try:
            urls, addrs, received_at = _message_meeting_urls(token, mid)
            for url in urls:
                out.append((mid, url, addrs, received_at))
        except Exception:
            pass
    return out
