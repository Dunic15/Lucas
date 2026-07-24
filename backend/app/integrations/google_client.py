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
from concurrent.futures import ThreadPoolExecutor
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import quote

import httpx

from .. import store
from ..config import settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Same collection endpoint serves events.insert (POST) and events.list (GET).
_CAL_INSERT = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_CAL_LIST = _CAL_INSERT
_CAL_CALENDARS = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
_FREEBUSY = "https://www.googleapis.com/calendar/v3/freeBusy"
_CAL_SETTINGS_TZ = "https://www.googleapis.com/calendar/v3/users/me/settings/timezone"
_GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_TIMEOUT = 30.0
# The upcoming-events read fans out over every accessible calendar in the
# connected Google account, not just primary or calendars currently checked in
# Google's sidebar. calendarList is fully paginated and event reads use a small
# worker pool so a complete account does not become a long serial request.
_CALENDAR_READ_WORKERS = 8


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


def _event_dedupe_key(item: dict) -> str:
    """Identify the same occurrence across calendars without collapsing a series.

    Google expands recurring events when singleEvents=true, but every occurrence
    keeps the series iCalUID. Pairing it with originalStartTime preserves Tuesday,
    Wednesday, etc. while still deduping an invited/shared-calendar copy of the
    same occurrence. For non-recurring events, start is the occurrence identity.
    """
    uid = str(item.get("iCalUID") or "")
    if not uid:
        return str(item.get("id") or "")
    occurrence = item.get("originalStartTime") or item.get("start") or {}
    raw = str(occurrence.get("dateTime") or occurrence.get("date") or "")
    if not raw:
        return uid
    # Equivalent copies can express the same instant with different UTC offsets.
    if "T" in raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                raw = dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return f"{uid}|{raw}"

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


def freebusy(
    principal: str, calendar_ids: list[str], window_start: str, window_end: str, *,
    oauth: dict | None = None, on_rotate=None, timezone: str = "UTC",
) -> dict:
    """Query Google's freeBusy for several calendars in ONE call — the read half
    of the find-a-time scheduler. OFF the live hot path (finalize / dashboard).

    Returns ``{ok, timezone, calendars: {id: {status, busy}}}`` where status is
    ``readable`` (with busy=[{start,end}] RFC3339), ``no_permission`` (Google
    returned an error for the calendar — usually a missing free/busy grant), or
    ``no_account`` (Google didn't return the calendar at all). NEVER raises: a
    token/network failure returns ``{ok:False, error, calendars:{}}`` so the
    caller degrades to "couldn't check" rather than treating a calendar as free.
    Reads via the SAME token seam as create_calendar_event — the ORG token by
    default (the calendar the event will actually be booked on)."""
    ids = [c for c in (calendar_ids or []) if c]
    if not ids:
        return {"ok": True, "timezone": timezone, "calendars": {}}
    key = principal
    token, err = _access_token(key, oauth, on_rotate=on_rotate)
    if err:
        return {"ok": False, "error": err, "calendars": {}}

    def _post(tok: str):
        return httpx.post(
            _FREEBUSY,
            headers={"Authorization": f"Bearer {tok}"},
            json={
                "timeMin": window_start, "timeMax": window_end,
                "timeZone": timezone, "items": [{"id": c} for c in ids],
            },
            timeout=_TIMEOUT,
        )

    try:
        resp = _post(token)
        if resp.status_code == 401:
            _drop_cached_token(key)  # revoked early — re-mint on retry
        if resp.status_code == 403:
            # Stale cached token from before a scope-adding reconnect — re-mint
            # once from the current refresh token (same self-heal as the
            # calendarList read). freeBusy rides calendar.readonly.
            _drop_cached_token(key)
            fresh, ferr = _access_token(
                key, oauth, on_rotate=on_rotate, force_refresh=True
            )
            if not ferr and fresh and fresh != token:
                resp = _post(fresh)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"freebusy request failed ({type(e).__name__})",
                "calendars": {}}
    if resp.status_code >= 300:
        print(f"[calendar] freeBusy HTTP {resp.status_code}", flush=True)
        return {"ok": False, "error": f"freebusy HTTP {resp.status_code}",
                "calendars": {}}

    cals = (resp.json() or {}).get("calendars") or {}
    out: dict[str, dict] = {}
    for cid in ids:
        entry = cals.get(cid)
        if entry is None:
            # Google didn't return the calendar — no such account we can see.
            out[cid] = {"status": "no_account", "busy": []}
        elif entry.get("errors"):
            # A returned error (commonly reason 'notFound') = free/busy not
            # visible to this token. Never treat as free.
            out[cid] = {"status": "no_permission", "busy": []}
        else:
            busy = [
                {"start": str(b.get("start") or ""), "end": str(b.get("end") or "")}
                for b in (entry.get("busy") or []) if isinstance(b, dict)
            ]
            out[cid] = {"status": "readable", "busy": busy}
    return {"ok": True, "timezone": timezone, "calendars": out}


def resolve_timezone(
    principal: str, *, oauth: dict | None = None, on_rotate=None,
) -> str:
    """The organizer calendar's IANA timezone (users/me/settings/timezone), or
    "" when it can't be read. Off the hot path; never raises. The scheduler
    falls back to UTC on "" — but resolving the real zone is what keeps
    "9-18 working hours" from meaning 4am for a non-UTC user."""
    token, err = _access_token(principal, oauth, on_rotate=on_rotate)
    if err:
        return ""
    try:
        resp = httpx.get(
            _CAL_SETTINGS_TZ, headers={"Authorization": f"Bearer {token}"},
            timeout=min(_TIMEOUT, 8.0),
        )
        if resp.status_code == 401:
            _drop_cached_token(principal)
        if resp.status_code >= 300:
            return ""
        return str((resp.json() or {}).get("value") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


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


def add_calendar_event_attendee(
    org_id: str, calendar_id: str, event_id: str, attendee_email: str, *,
    oauth: dict | None = None, principal: str = "", on_rotate=None,
) -> dict:
    """Add one attendee to an existing Google event without losing its guests.

    The dashboard passes a signed event reference which resolves to the calendar
    and event ids, plus the caller's own OAuth principal. Google remains the
    authorization boundary: a user who cannot edit the event gets a soft 403
    result, never a cross-account write.
    """
    calendar_id = str(calendar_id or "").strip()
    event_id = str(event_id or "").strip()
    attendee_email = str(attendee_email or "").strip().lower()
    if not calendar_id or not event_id or not attendee_email:
        return {"ok": False, "error": "calendar, event and attendee are required"}

    key = principal or org_id
    token, err = _access_token(key, oauth, on_rotate=on_rotate)
    if err:
        return {"ok": False, "error": err}
    headers = {"Authorization": f"Bearer {token}"}
    event_url = f"{_cal_events_url(calendar_id)}/{quote(event_id, safe='')}"
    try:
        current = httpx.get(event_url, headers=headers, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"calendar event read failed ({type(e).__name__})"}
    if current.status_code == 401:
        _drop_cached_token(key)
    if current.status_code >= 300:
        return {
            "ok": False,
            "error": f"calendar event read failed (HTTP {current.status_code})",
        }

    current_data = current.json()
    raw_attendees = current_data.get("attendees", [])
    allowed = {
        "email", "displayName", "optional", "responseStatus",
        "comment", "additionalGuests", "resource",
    }
    attendees: list[dict] = []
    present: set[str] = set()
    for raw in raw_attendees if isinstance(raw_attendees, list) else []:
        if not isinstance(raw, dict):
            continue
        email = str(raw.get("email") or "").strip()
        if not email:
            continue
        present.add(email.lower())
        attendees.append({k: v for k, v in raw.items() if k in allowed})
    if attendee_email in present:
        return {
            "ok": True,
            "event_id": event_id,
            "event_url": str(current_data.get("htmlLink") or ""),
            "idempotent": True,
        }
    attendees.append({"email": attendee_email})
    try:
        patched = httpx.patch(
            event_url,
            params={"sendUpdates": "all"},
            headers=headers,
            json={"attendees": attendees},
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"calendar event update failed ({type(e).__name__})"}
    if patched.status_code == 401:
        _drop_cached_token(key)
    if patched.status_code >= 300:
        return {
            "ok": False,
            "error": f"calendar event update failed (HTTP {patched.status_code})",
        }
    data = patched.json()
    return {
        "ok": True,
        "event_id": str(data.get("id") or event_id),
        "event_url": str(data.get("htmlLink") or ""),
        "idempotent": False,
    }


def list_calendar_events(
    org_id: str, *, max_results: int = 20,
    oauth: dict | None = None, principal: str = "", on_rotate=None,
    time_min: str = "", time_max: str = "",
) -> dict:
    """List UPCOMING events across every accessible account calendar (read-only).

    Mirrors ``create_calendar_event`` / ``send_gmail``: mint a short-lived access
    token, fully paginate the user's accessible calendarList (fail-soft to
    primary-only), then fully paginate each calendar's events for bounded views
    with ``timeMin``, ``singleEvents=true`` and ``orderBy=startTime``;
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
    # calendarList is paginated to exhaustion: no arbitrary account-size cap.
    # Any failure on the first page still degrades to primary-only; a later-page
    # failure keeps the calendars already discovered.
    cal_ids = ["primary"]
    cal_meta: dict[str, dict[str, str | bool]] = {
        "primary": {
            "id": "primary", "name": "Primary calendar", "color": "", "primary": True
        }
    }
    if not _calendarlist_blocked(key):
        try:
            def _calendar_page(page_token: str = ""):
                params = {
                    "maxResults": 250,
                    "showDeleted": "false",
                    "fields": (
                        "nextPageToken,items(id,summary,selected,primary,"
                        "backgroundColor,deleted,accessRole)"
                    ),
                }
                if page_token:
                    params["pageToken"] = page_token
                return httpx.get(
                    _CAL_CALENDARS,
                    params=params,
                    headers=headers,
                    timeout=min(_TIMEOUT, 8.0),
                )

            listing = _calendar_page()
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
                    listing = _calendar_page()
            if listing.status_code >= 300:
                print(f"[calendar] calendarList HTTP {listing.status_code}", flush=True)
                _note_calendarlist_result(key, ok=False)
            else:
                _note_calendarlist_result(key, ok=True)
                raw_cals: list[dict] = []
                while True:
                    payload = listing.json()
                    page_items = payload.get("items", [])
                    if isinstance(page_items, list):
                        raw_cals.extend(
                            cal for cal in page_items if isinstance(cal, dict)
                        )
                    page_token = str(payload.get("nextPageToken") or "")
                    if not page_token:
                        break
                    listing = _calendar_page(page_token)
                    if listing.status_code >= 300:
                        # Keep already-discovered pages. The diagnostic contains
                        # no account, calendar, token, or event content.
                        print(
                            f"[calendar] calendarList page HTTP {listing.status_code}",
                            flush=True,
                        )
                        break

                cals = [
                    cal
                    for cal in raw_cals
                    if cal.get("id")
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
                if cals:
                    cal_ids = [str(cal["id"]) for cal in cals]
                    cal_meta = {
                        str(cal["id"]): {
                            "id": str(cal["id"]),
                            "name": str(
                                cal.get("summary")
                                or ("Primary calendar" if cal.get("primary") else "Calendar")
                            ),
                            "color": str(cal.get("backgroundColor") or ""),
                            "primary": bool(cal.get("primary")),
                        }
                        for cal in cals
                    }
        except Exception:  # noqa: BLE001 — calendarList is best-effort by design
            pass

    merged: list[dict] = []
    seen: set[str] = set()
    first_error = ""

    def _read_calendar(cid: str) -> tuple[str, list[dict], str]:
        # A selected window is a complete calendar view, not a preview: consume
        # every events.list page. Google may return a short or even empty page
        # while still supplying nextPageToken. Open-ended reads remain bounded
        # to n because they feed the compact meeting brief too.
        base_params = {
            "timeMin": time_min or datetime.now(timezone.utc).isoformat(),
            "singleEvents": "true",
            # Google omits invitations hidden by the account's invitation
            # settings unless this is explicit. The Calendar UI can still show
            # them, so a replica must request them too.
            "showHiddenInvitations": "true",
            "orderBy": "startTime",
            "maxResults": n,
        }
        if time_max:
            base_params["timeMax"] = time_max
        items: list[dict] = []
        page_token = ""
        while True:
            params = dict(base_params)
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = httpx.get(
                    _cal_events_url(cid),
                    params=params,
                    headers=headers,
                    timeout=min(_TIMEOUT, 8.0),
                )
            except Exception as e:  # noqa: BLE001
                if items:
                    break
                return cid, [], f"calendar list failed ({type(e).__name__})"
            if resp.status_code == 401:
                _drop_cached_token(key)  # revoked early — next attempt re-mints
            if resp.status_code >= 300:
                print(f"[calendar] list HTTP {resp.status_code}", flush=True)
                if items:
                    break
                return cid, [], f"calendar list failed (HTTP {resp.status_code})"
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                if items:
                    break
                return cid, [], "calendar list returned no JSON"
            page_items = payload.get("items", [])
            if isinstance(page_items, list):
                items.extend(page_items)
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                break
            if not time_max and len(items) >= n:
                break
        return cid, (items if time_max else items[:n]), ""

    # Fetch every calendar, but never serially hammer Google. pool.map preserves
    # calendar priority, which also makes iCalUID dedupe deterministic.
    workers = min(_CALENDAR_READ_WORKERS, max(1, len(cal_ids)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(_read_calendar, cal_ids)
        for cid, items, read_error in results:
            if read_error:
                first_error = first_error or read_error
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                # Deduplicate the same occurrence across calendars. Recurring
                # instances share iCalUID, so the occurrence time is essential.
                dk = _event_dedupe_key(it)
                if dk in seen:
                    continue
                if dk:
                    seen.add(dk)
                decorated = dict(it)
                # Internal display metadata: dashboard.py distils this into a
                # calendar label/color; the raw calendar id is never exposed.
                decorated["_laura_calendar"] = cal_meta.get(
                    cid, {
                        "id": cid, "name": "Calendar", "color": "",
                        "primary": cid == "primary",
                    }
                )
                merged.append(decorated)

    if not merged and first_error:
        return {"ok": False, "error": first_error}
    merged.sort(key=_event_start_ts)
    # A bounded calendar window is exhaustive; only the open-ended brief/list is
    # intentionally capped. Truncating a two-week view here hides later events.
    if not time_max:
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


def verify_calendar_event(org_id: str, event_id: str) -> bool:
    """Read-back check: does the event we just wrote actually exist? Phase-1
    evidence for the NATIVE plane (the Pipedream plane already verifies).
    Best-effort — never raises, False on any doubt."""
    eid = str(event_id or "").strip()
    if not eid:
        return False
    token, err = _access_token(org_id)
    if err or not token:
        return False
    try:
        import httpx

        resp = httpx.get(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{eid}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def verify_gmail_message(org_id: str, message_id: str) -> bool:
    """Read-back check for a just-sent Gmail message. Best-effort."""
    mid = str(message_id or "").strip()
    if not mid:
        return False
    token, err = _access_token(org_id)
    if err or not token:
        return False
    try:
        import httpx

        resp = httpx.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=minimal",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


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
    if not res.get("ok"):
        # Native first, Pipedream second (owner call 2026-07-22): an org whose
        # Google lives ONLY in Connect still gets calendar sight at join —
        # without this, the avatar had no data and improvised "I'll check it"
        # promises all call long.
        try:
            from .. import pipedream_executor

            pd = pipedream_executor.read_calendar_events(
                org, max_results=_BRIEF_MAX_EVENTS
            )
            if pd.get("ok"):
                res = pd
        except Exception:  # noqa: BLE001 — the join never fails on a brief
            pass
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


# ── Gmail inbox brief (owner ask 2026-07-24: "what's on my inbox?" had NO
# read path at all — neither at join nor live). Same shape as calendar_brief:
# a join-time snapshot riding memory_brief, HEADERS ONLY (from/subject/unread,
# never bodies), TTL-cached, never negative-cached, and the join never fails
# on it. Native OAuth first (gmail.readonly is in the connect scopes,
# api/oauth.py), Pipedream Connect proxy second. ──────────────────────────
_INBOX_BRIEF_MAX = 8
_inbox_brief_cache: dict[str, tuple[float, str]] = {}


def list_inbox_messages(org_id: str, *, max_results: int = 8) -> dict:
    """Latest INBOX message headers via the org's native Google OAuth.
    Returns {"ok": True, "messages": [{from, subject, unread}]} or
    {"ok": False, "error": str}. Never raises; logs status only — no sender,
    subject or token ever reaches a log line (transcript-grade PII rules)."""
    org = (org_id or "").strip()
    token, err = _access_token(org)
    if err:
        return {"ok": False, "error": err}
    try:
        n = max(1, min(int(max_results or 8), 15))
    except (TypeError, ValueError):
        n = 8
    headers = {"Authorization": f"Bearer {token}"}
    base = "https://gmail.googleapis.com/gmail/v1/users/me"
    try:
        listing = httpx.get(
            f"{base}/messages",
            params={"maxResults": n, "labelIds": "INBOX"},
            headers=headers, timeout=_TIMEOUT,
        )
        if listing.status_code != 200:
            # 403 = pre-scope-change grant without gmail.readonly — the caller
            # falls through to the Pipedream read, exactly like calendar.
            print(f"[gmail] inbox read blocked: HTTP {listing.status_code}",
                  flush=True)
            return {"ok": False, "error": f"HTTP {listing.status_code}"}
        ids = [
            str(m.get("id") or "")
            for m in (listing.json() or {}).get("messages") or []
            if isinstance(m, dict) and m.get("id")
        ][:n]
        messages: list[dict] = []
        for mid in ids:
            one = httpx.get(
                f"{base}/messages/{mid}",
                params={"format": "metadata",
                        "metadataHeaders": ["From", "Subject"]},
                headers=headers, timeout=_TIMEOUT,
            )
            if one.status_code != 200:
                continue  # best-effort per message
            payload = one.json() or {}
            hmap = {
                str(h.get("name") or "").lower(): str(h.get("value") or "")
                for h in ((payload.get("payload") or {}).get("headers") or [])
                if isinstance(h, dict)
            }
            messages.append({
                "from": hmap.get("from", ""),
                "subject": hmap.get("subject", ""),
                "unread": "UNREAD" in (payload.get("labelIds") or []),
            })
        return {"ok": True, "messages": messages}
    except Exception as exc:  # noqa: BLE001 — a read must degrade, never raise
        return {"ok": False, "error": type(exc).__name__}


def _inbox_line(m: dict) -> str:
    """One distilled, size-capped brief line: sender display + subject."""
    sender = str(m.get("from") or "")
    # keep the display name, drop the <addr> part when a name exists
    disp = re.sub(r"\s*<[^>]*>", "", sender).strip().strip('"') or sender
    subject = str(m.get("subject") or "(no subject)")
    flag = " [unread]" if m.get("unread") else ""
    return f"• {disp[:40]} — {subject[:70]}{flag}"


def gmail_inbox_brief(org_id: str) -> str:
    """Markdown snapshot of the org inbox for the meeting brief; "" when Gmail
    is unavailable. Sync (network) — call via run_in_threadpool at session
    start only. TTL-cached per org; an empty/transient read is NEVER cached
    (the asana_client lesson: one hiccup must not poison the whole TTL)."""
    org = (org_id or "").strip()
    if not org:
        return ""
    now = time.time()
    cached = _inbox_brief_cache.get(org)
    if cached and now - cached[0] < _BRIEF_TTL:
        return cached[1]
    res = list_inbox_messages(org, max_results=_INBOX_BRIEF_MAX)
    if not res.get("ok"):
        try:
            from .. import pipedream_executor

            pd = pipedream_executor.read_gmail_inbox(
                org, max_results=_INBOX_BRIEF_MAX
            )
            if pd.get("ok"):
                res = pd
        except Exception:  # noqa: BLE001 — the join never fails on a brief
            pass
    brief = ""
    if res.get("ok"):
        msgs = [m for m in (res.get("messages") or []) if isinstance(m, dict)]
        if msgs:
            unread = sum(1 for m in msgs if m.get("unread"))
            lines = [f"(latest {len(msgs)}, {unread} unread — as of meeting start)"]
            lines += [_inbox_line(m) for m in msgs]
            text = "\n".join(lines)
            if len(text) > _BRIEF_MAX_CHARS:
                text = text[:_BRIEF_MAX_CHARS].rsplit("\n", 1)[0]
            brief = text
    if brief:
        _inbox_brief_cache[org] = (now, brief)
    else:
        _inbox_brief_cache.pop(org, None)
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
