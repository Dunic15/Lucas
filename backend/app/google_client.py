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
import threading
import time
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import quote

import httpx

from . import store
from .config import settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Same collection endpoint serves events.insert (POST) and events.list (GET).
_CAL_INSERT = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_CAL_LIST = _CAL_INSERT
_CAL_CALENDARS = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
_GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_TIMEOUT = 30.0
# The upcoming-events read fans out over every accessible calendar in the
# connected Google account, not just primary or calendars currently checked in
# Google's sidebar. This is a dashboard/week-view read; the compact in-meeting
# brief still trims the merged result after the fan-out. The cap protects an
# unusually large account from unbounded Google round-trips while covering the
# normal case (and fixes the old first-8-selected-calendars truncation).
_MAX_CALENDARS = 32
_LIST_DEADLINE_S = 30.0


def _cal_events_url(cal_id: str) -> str:
    """events.list URL for one calendar (ids contain '@' and '#' — quote them)."""
    return f"https://www.googleapis.com/calendar/v3/calendars/{quote(cal_id, safe='')}/events"


def _event_start_ts(item: dict) -> float:
    """Sortable start of a Google event item; parse failures sort last. Mixed
    tz-aware dateTime and all-day date values normalize to UTC timestamps —
    plain string compare would misorder 'Z' vs '+02:00' offsets."""
    s = item.get("start") or {}
    raw = str(s.get("dateTime") or s.get("date") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()

# ── per-principal access-token cache ──
# One Google token round-trip per principal per ~hour instead of one per API
# call. A principal is an org_id (native executor / demo) or "user:<user_id>"
# (per-user dashboard calendar) — distinct keys, so a member's token never
# shares a slot with the org's or a colleague's. Values live only in this
# process and are never persisted or logged; entries expire shortly before
# Google's stated expiry, and a 401 from an API call drops the entry so
# revocation heals on the next attempt, not in an hour.
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}  # principal -> (token, expires_at)
_EXPIRY_MARGIN_S = 120.0  # re-mint this long before Google's stated expiry
_DEFAULT_TTL_S = 3300.0  # expires_in missing → assume just under Google's 1h


# Principals whose calendarList is confirmed 403 even after a fresh mint —
# the grant genuinely lacks calendar.readonly (a pre-scope-change connection
# that never reconnected) or a Workspace admin blocks the app. Without this,
# EVERY read for such a principal would re-pay the fan-out's extra token mint +
# calendarList GET forever (dashboard loads AND the session-start calendar
# brief, ~every 3 min per active org). We remember the block briefly and go
# straight to primary-only; a reconnect (which drops the token cache) clears
# it, so a user who grants the scope re-checks on their very next read.
_CALLIST_BLOCK: dict[str, float] = {}
_CALLIST_BLOCK_TTL = 300.0


def _reset_token_cache() -> None:
    """Test seam — process-global state, cleared per test (see conftest)."""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()
        _CALLIST_BLOCK.clear()


def _drop_cached_token(org_id: str) -> None:
    with _TOKEN_LOCK:
        _TOKEN_CACHE.pop(org_id, None)
        # A reconnect drops the token here; also forget any calendarList block
        # so the newly-granted scope is re-checked on the next read, not after
        # the TTL. Harmless when called from the 401 path (just re-checks once).
        _CALLIST_BLOCK.pop(org_id, None)


def _calendarlist_blocked(principal: str) -> bool:
    with _TOKEN_LOCK:
        exp = _CALLIST_BLOCK.get(principal)
        if exp is None:
            return False
        if exp <= time.time():
            _CALLIST_BLOCK.pop(principal, None)
            return False
        return True


def _note_calendarlist_result(principal: str, ok: bool) -> None:
    """Remember a surviving 403 (skip the fan-out for a bit) or clear the block
    when calendarList works again."""
    with _TOKEN_LOCK:
        if ok:
            _CALLIST_BLOCK.pop(principal, None)
        else:
            _CALLIST_BLOCK[principal] = time.time() + _CALLIST_BLOCK_TTL


def _access_token(
    principal: str, oauth: dict | None = None, *, on_rotate=None,
    force_refresh: bool = False,
) -> tuple[str, str]:
    """(access_token, "") for a principal, or ("", error) when unavailable.

    ``principal`` is the token-CACHE key. By default it is an ``org_id`` and the
    refresh token is resolved from ``store.get_org_oauth`` — the native-executor
    path, unchanged. A caller serving a PER-USER surface passes a pre-resolved
    ``oauth`` dict (e.g. ``store.get_user_oauth``) plus a DISTINCT ``principal``
    (e.g. ``"user:<user_id>"``), so one person's calendar token never shares a
    cache slot with a colleague's. Refresh-token rotation is persisted back to
    org_oauth for the org path, or via ``on_rotate(new_rt)`` for a supplied
    (per-user) token — never discarded (that would strand a revoked credential).

    ``force_refresh`` bypasses the cache and re-mints from the CURRENT stored
    refresh token. A reconnect that only ADDS a scope leaves the old, still
    time-valid access token in the cache (old scopes) — so a scope-gated call
    (calendarList needs calendar.readonly) keeps 403ing for up to an hour, on
    every instance whose cache holds it. The 403 self-heal in
    list_calendar_events sets this to re-mint with the new grant immediately."""
    if not settings.google_calendar_client_id or not settings.google_calendar_client_secret:
        return "", "Google OAuth client is not configured"
    now = time.time()
    if not force_refresh:
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(principal)
            if cached and cached[1] > now:
                return cached[0], ""
    resolved_from_org = oauth is None
    if resolved_from_org:
        oauth = store.get_org_oauth(principal)
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
    data = resp.json()
    tok = data.get("access_token", "")
    if not tok:
        return "", "no access token returned"
    try:
        ttl = float(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        ttl = 0.0
    if ttl <= 0:
        ttl = _DEFAULT_TTL_S
    with _TOKEN_LOCK:
        _TOKEN_CACHE[principal] = (tok, now + max(60.0, ttl - _EXPIRY_MARGIN_S))
    # Refresh-token rotation: providers may return a NEW refresh token with
    # the access token (Google does under rotation policies). Discarding it
    # strands the credential — persist it like connect does, keeping the row's
    # email/scopes. Org path writes org_oauth; a supplied (per-user) token is
    # rewritten by its owner via on_rotate.
    new_rt = str(data.get("refresh_token") or "").strip()
    if new_rt and new_rt != oauth["refresh_token"]:
        if resolved_from_org:
            store.set_org_oauth(
                principal, new_rt,
                email=str(oauth.get("email") or ""),
                scopes=str(oauth.get("scopes") or ""),
            )
        elif on_rotate is not None:
            on_rotate(new_rt)
        # Keep the in-memory dict current: a second mint in the SAME request
        # (the calendarList-403 force_refresh) must redeem the ROTATED token,
        # not the one Google just retired.
        oauth["refresh_token"] = new_rt
    return tok, ""


def _emails(value: Any) -> list[str]:
    if isinstance(value, str):
        return [e.strip() for e in value.replace(";", ",").split(",") if e.strip()]
    if isinstance(value, (list, tuple)):
        return [str(e).strip() for e in value if str(e).strip()]
    return []


def create_calendar_event(
    org_id: str, event: dict, *,
    oauth: dict | None = None, principal: str = "", on_rotate=None,
) -> dict:
    """Create a Calendar event on the primary calendar (events.insert).

    ``event``: {title/summary, start (RFC3339), end (RFC3339), attendees?,
    description?, timezone?}. Attendees are invited (sendUpdates=all).

    Token resolves by ``org_id`` by default (native executor). A dashboard user
    scheduling on THEIR OWN calendar passes ``oauth``/``principal`` the same way
    as ``list_calendar_events`` — so a shared-org member creates events on their
    own calendar, not a colleague's."""
    summary = str(event.get("title") or event.get("summary") or "").strip()
    start = str(event.get("start") or "").strip()
    end = str(event.get("end") or "").strip()
    if not summary or not start or not end:
        return {"ok": False, "error": "event needs title, start and end"}
    tz = str(event.get("timezone") or event.get("time_zone") or "UTC").strip() or "UTC"

    key = principal or org_id
    token, err = _access_token(key, oauth, on_rotate=on_rotate)
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
    if resp.status_code == 401:
        _drop_cached_token(key)  # revoked early — next attempt re-mints
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


def list_calendar_events(
    org_id: str, *, max_results: int = 20,
    oauth: dict | None = None, principal: str = "", on_rotate=None,
    time_min: str = "", time_max: str = "",
) -> dict:
    """List UPCOMING events across the account's SELECTED calendars (read-only).

    Mirrors ``create_calendar_event`` / ``send_gmail``: mint a short-lived access
    token, list the user's calendarList (selected + primary, capped, fail-soft
    to primary-only), then GET each calendar's events with ``timeMin=now``,
    ``singleEvents=true``, ``orderBy=startTime`` and a small ``maxResults``;
    results are deduped on iCalUID (invited copy vs shared calendar) and merged
    in start order. The ``calendar.events.readonly`` scope is granted at connect.

    By default the token is resolved by ``org_id`` (native executor / demo view).
    The per-USER dashboard passes a resolved ``oauth`` dict + a distinct
    ``principal`` (cache key), so a member of a SHARED org only ever sees THEIR
    own calendar — never a colleague's.

    Returns ``{"ok": True, "events": [...raw Google items...]}`` (the caller
    distills title / start / attendees / meeting URL) or
    ``{"ok": False, "error": str}``. It NEVER raises: this feeds a dashboard READ
    off the live path, so a Google hiccup must degrade to an empty list, not a
    500. Logs no token and no event content."""
    key = principal or org_id
    token, err = _access_token(key, oauth, on_rotate=on_rotate)
    if err:
        # Diagnostic (status only, no token/email/event content): why a connected
        # org's calendar shows empty — token-refresh rejected, not connected, etc.
        print(f"[calendar] read blocked: {err}", flush=True)
        return {"ok": False, "error": err}
    try:
        n = int(max_results or 20)
    except (TypeError, ValueError):
        n = 20
    # A BOUNDED window (week navigation, time_max set) can safely pull more —
    # a busy shared "program" week can hold well over 50 events across several
    # calendars, and truncating there is exactly the "I don't see all my
    # standups" bug. Open-ended "from now" reads stay capped at 50.
    n = max(1, min(n, 200 if time_max else 50))
    headers = {"Authorization": f"Bearer {token}"}

    # Which calendars feed the view: EVERY accessible calendar in the signed-in
    # Google account (primary first, then calendars visible in Google's sidebar,
    # then the rest). "selected" is only a UI checkbox — filtering on it silently
    # omitted valid meetings, including Meet events on subscribed/team calendars.
    # calendarList allows 250 rows in one page; we then apply a defensive cap.
    # Any failure still degrades to primary-only, never to an error.
    cal_ids = ["primary"]
    cal_meta: dict[str, dict[str, str | bool]] = {
        "primary": {"name": "Primary calendar", "color": "", "primary": True}
    }
    if not _calendarlist_blocked(key):
        try:
            listing = httpx.get(
                _CAL_CALENDARS,
                params={
                    "maxResults": 250,
                    "showDeleted": "false",
                    "fields": (
                        "items(id,summary,selected,primary,backgroundColor,"
                        "deleted,accessRole)"
                    ),
                },
                headers=headers,
                timeout=min(_TIMEOUT, 8.0),
            )
            if listing.status_code == 403:
                # 403 = the token lacks calendar.readonly. Almost always a stale
                # CACHED access token from BEFORE a reconnect that added the
                # scope. Re-mint ONCE from the current refresh token and retry.
                _drop_cached_token(key)
                fresh, ferr = _access_token(
                    key, oauth, on_rotate=on_rotate, force_refresh=True
                )
                if not ferr and fresh and fresh != token:
                    token = fresh
                    headers = {"Authorization": f"Bearer {token}"}
                    listing = httpx.get(
                        _CAL_CALENDARS,
                        params={
                            "maxResults": 250,
                            "showDeleted": "false",
                            "fields": (
                                "items(id,summary,selected,primary,"
                                "backgroundColor,deleted,accessRole)"
                            ),
                        },
                        headers=headers,
                        timeout=min(_TIMEOUT, 8.0),
                    )
            if listing.status_code >= 300:
                print(f"[calendar] calendarList HTTP {listing.status_code}", flush=True)
                _note_calendarlist_result(key, ok=False)
            else:
                _note_calendarlist_result(key, ok=True)
                raw_cals = listing.json().get("items", [])
                cals = [
                    cal
                    for cal in (raw_cals if isinstance(raw_cals, list) else [])
                    if isinstance(cal, dict)
                    and cal.get("id")
                    and not cal.get("deleted")
                    and str(cal.get("accessRole") or "") != "none"
                ]
                # Stable priority: primary, then calendars the person currently
                # shows in Google Calendar, then hidden/subscribed calendars.
                cals.sort(
                    key=lambda cal: (
                        0 if cal.get("primary") else 1,
                        0 if cal.get("selected") else 1,
                    )
                )
                picked = cals[:_MAX_CALENDARS]
                if picked:
                    cal_ids = [str(cal["id"]) for cal in picked]
                    cal_meta = {
                        str(cal["id"]): {
                            "name": str(
                                cal.get("summary")
                                or ("Primary calendar" if cal.get("primary") else "Calendar")
                            ),
                            "color": str(cal.get("backgroundColor") or ""),
                            "primary": bool(cal.get("primary")),
                        }
                        for cal in picked
                    }
        except Exception:  # noqa: BLE001 — calendarList is best-effort by design
            pass

    merged: list[dict] = []
    seen: set[str] = set()
    first_error = ""
    deadline = time.monotonic() + _LIST_DEADLINE_S
    for cid in cal_ids:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break  # budget spent — return what we have, off-path callers retry
        _params = {
            "timeMin": time_min or datetime.now(timezone.utc).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": n,
        }
        if time_max:  # bound the window (week navigation) — else open-ended
            _params["timeMax"] = time_max
        try:
            resp = httpx.get(
                _cal_events_url(cid),
                params=_params,
                headers=headers,
                timeout=min(_TIMEOUT, max(2.0, remaining)),
            )
        except Exception as e:  # noqa: BLE001
            first_error = first_error or f"calendar list failed ({type(e).__name__})"
            continue
        if resp.status_code == 401:
            _drop_cached_token(key)  # revoked early — next attempt re-mints
        if resp.status_code >= 300:
            # Diagnostic: a 403 here on a Workspace domain usually = admin/API
            # access restriction on the (unverified) app; 401 = token/scope.
            print(f"[calendar] list HTTP {resp.status_code}", flush=True)
            first_error = first_error or f"calendar list failed (HTTP {resp.status_code})"
            continue
        try:
            items = resp.json().get("items", [])
        except Exception:  # noqa: BLE001
            first_error = first_error or "calendar list returned no JSON"
            continue
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            # The same meeting shows up on several calendars (invited copy +
            # a shared calendar) — iCalUID is stable across those copies.
            dk = str(it.get("iCalUID") or it.get("id") or "")
            if dk in seen:
                continue
            if dk:
                seen.add(dk)
            decorated = dict(it)
            # Internal display metadata: dashboard.py distils this into a
            # calendar label/color; the raw calendar id is never exposed.
            decorated["_laura_calendar"] = cal_meta.get(
                cid, {"name": "Calendar", "color": "", "primary": cid == "primary"}
            )
            merged.append(decorated)
    if not merged and first_error:
        return {"ok": False, "error": first_error}
    merged.sort(key=_event_start_ts)
    merged = merged[:n]
    # Diagnostic: items=0 on a connected org = right token but no events in the
    # window (wrong account/calendar), vs a non-200 above = an API/auth failure.
    print(
        f"[calendar] list ok items={len(merged)} calendars={len(cal_ids)}",
        flush=True,
    )
    return {"ok": True, "events": merged}


# ── calendar brief: the avatar's read-side calendar sight ────────────────────
# Same discipline as drive_client.folder_brief: assembled at session start off
# the live path, best-effort ("" on any failure), bounded (it rides the live
# prompt every turn), cached per org for a few minutes, event content never
# logged (counts only — list_calendar_events already follows this).

_BRIEF_TTL = 180.0
_BRIEF_MAX_EVENTS = 8
_BRIEF_MAX_CHARS = 900
_brief_cache: dict[str, tuple[float, str]] = {}


def _event_line(item: dict) -> str:
    """One compact line: '- Mon 21 Jul 14:00–14:30 — Weekly Planning (with A, B)'."""
    start = item.get("start") or {}
    end = item.get("end") or {}
    title = str(item.get("summary") or "(no title)").strip()
    when = ""
    raw_start = str(start.get("dateTime") or "")
    if raw_start:
        try:
            dt = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
            when = dt.strftime("%a %d %b %H:%M")
            raw_end = str(end.get("dateTime") or "")
            if raw_end:
                try:
                    when += "–" + datetime.fromisoformat(
                        raw_end.replace("Z", "+00:00")
                    ).strftime("%H:%M")
                except ValueError:
                    pass
        except ValueError:
            when = raw_start
    elif start.get("date"):
        when = f"{start['date']} (all day)"
    guests = [
        str(a.get("displayName") or a.get("email") or "").split("@")[0]
        for a in (item.get("attendees") or [])
        if isinstance(a, dict) and not a.get("self") and not a.get("resource")
    ]
    guests = [g for g in guests if g]
    extra = ""
    if guests:
        shown = ", ".join(guests[:3])
        more = f" +{len(guests) - 3}" if len(guests) > 3 else ""
        extra = f" (with {shown}{more})"
    return f"- {when} — {title}{extra}" if when or title else ""


def calendar_brief(org_id: str) -> str:
    """Markdown brief of the org's upcoming primary calendar; "" when the org
    has no Google connected or on any failure — the join proceeds without it."""
    org = (org_id or "").strip()
    if not org:
        return ""
    now = time.time()
    cached = _brief_cache.get(org)
    if cached and now - cached[0] < _BRIEF_TTL:
        return cached[1]
    res = list_calendar_events(org, max_results=_BRIEF_MAX_EVENTS)
    brief = ""
    if res.get("ok"):
        lines = [
            line
            for item in (res.get("events") or [])[:_BRIEF_MAX_EVENTS]
            if isinstance(item, dict) and (line := _event_line(item))
        ]
        text = "\n".join(lines)
        if len(text) > _BRIEF_MAX_CHARS:
            text = text[:_BRIEF_MAX_CHARS].rsplit("\n", 1)[0]
        brief = text
    _brief_cache[org] = (now, brief)
    return brief


def _rfc822_id(value: Any) -> str:
    """Normalize a Message-ID to its angle-bracketed RFC 822 form ('' if empty)."""
    mid = str(value or "").strip()
    if not mid:
        return ""
    return mid if mid.startswith("<") and mid.endswith(">") else f"<{mid}>"


def send_gmail(org_id: str, message: dict) -> dict:
    """Send an email as the org's Google account (Gmail messages.send).

    ``message``: {to (str|list of emails), subject, body, cc?, bcc?,
    html_body?, thread_id?, in_reply_to?}. All extras are optional and the
    plain {to, subject, body} call is byte-identical to before:
    - ``html_body`` → multipart/alternative (plaintext part first, so clients
      that prefer HTML render it and plain-text clients still get the body).
    - ``thread_id`` (Gmail's threadId) + ``in_reply_to`` (the RFC 822
      Message-ID being answered) turn the send into a real reply: Gmail
      threads on threadId, strict clients thread on In-Reply-To/References.
    - ``bcc`` rides in the raw MIME; Gmail strips the header on delivery."""
    to = _emails(message.get("to"))
    cc = _emails(message.get("cc"))
    bcc = _emails(message.get("bcc"))
    subject = str(message.get("subject") or "").strip()
    text = str(message.get("body") or message.get("text") or "")
    html = str(message.get("html_body") or message.get("html") or "")
    if not to:
        return {"ok": False, "error": "email needs at least one recipient"}
    if not subject and not text and not html:
        return {"ok": False, "error": "email needs a subject or a body"}

    token, err = _access_token(org_id)
    if err:
        return {"ok": False, "error": err}

    if html:
        mime: Any = MIMEMultipart("alternative")
        mime.attach(MIMEText(text, "plain", _charset="utf-8"))
        mime.attach(MIMEText(html, "html", _charset="utf-8"))
    else:
        mime = MIMEText(text, _charset="utf-8")
    mime["To"] = ", ".join(to)
    if cc:
        mime["Cc"] = ", ".join(cc)
    if bcc:
        mime["Bcc"] = ", ".join(bcc)
    mime["Subject"] = subject
    in_reply_to = _rfc822_id(message.get("in_reply_to"))
    if in_reply_to:
        mime["In-Reply-To"] = in_reply_to
        mime["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    payload: dict[str, Any] = {"raw": raw}
    thread_id = str(message.get("thread_id") or "").strip()
    if thread_id:
        payload["threadId"] = thread_id

    try:
        resp = httpx.post(
            _GMAIL_SEND,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"gmail request failed ({type(e).__name__})"}
    if resp.status_code == 401:
        _drop_cached_token(org_id)  # revoked early — next attempt re-mints
    if resp.status_code >= 300:
        return {"ok": False, "error": f"gmail send failed (HTTP {resp.status_code})"}
    data = resp.json()
    return {
        "ok": True,
        "message_id": data.get("id", ""),
        "thread_id": data.get("threadId", ""),
    }
