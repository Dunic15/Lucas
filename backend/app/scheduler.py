"""Find-a-time scheduler — the PRODUCER half of the calendar flow.

Turns a vague scheduling ask ("book a 45-min with Ananth next week") into a
``CalendarProposal`` (candidate slots the existing approve doors can act on).
The CONSUMER — selecting a slot and creating the event — already lives on the
approve doors (org_api.py / dashboard.py); this only produces the proposal.

Design rules:
- OFF the live meeting hot path: every function here runs at finalize or an
  explicit dashboard request, never on ws/<conversation_id> (latency is the
  product on the live path).
- Deterministic-first: the ranker and constraint parser are pure stdlib
  (datetime + zoneinfo), fully offline-testable, no LLM.
- Never raises: a bad token / network hiccup / unparseable time degrades to an
  empty proposal, never an exception and never "treated as free".
- Inert until ``settings.scheduler_find_time`` is on: with the flag off nothing
  attaches a proposal, so every downstream door is byte-identical to today.

Emitted slot times are NAIVE-LOCAL ISO (``2026-07-20T09:00:00``, no offset) in
``proposal.timezone`` — the exact format the shipped staleness check and
approve-door tests require; create_calendar_event re-attaches the zone.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_LOCAL_FMT = "%Y-%m-%dT%H:%M:%S"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Scheduling intent — a vague ask that should become a proposal (the explicit-
# time asks are already typed by brain.type_actions; enrich only sees untyped).
_SCHED_RE = re.compile(
    # a scheduling verb, then — within a short span — a meeting noun OR "with
    # <someone>" OR a duration. The attendee gate in enrich_actions is the real
    # filter, so the intent match can afford to be liberal here.
    r"\b(?:schedule|book|set\s?up|arrange|find\s+(?:a\s+)?time|put\s+together)\b"
    r".{0,60}?"
    r"(?:\b(?:meeting|call|sync|catch[\s-]?up|follow[\s-]?up|1[:\s]?1|"
    r"one[\s-]?on[\s-]?one|chat|session|invite)\b"
    r"|\bwith\b"
    r"|\b\d+\s*(?:min|minute|hour|hr|h)s?\b"
    r"|\ban?\s+hour\b)"
    r"|\b(?:meeting|call|sync)\s+with\b",
    re.IGNORECASE,
)
_ISOISH_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
# time-of-day buckets, local hours [start, end)
_TOD = {"morning": (9, 12), "afternoon": (12, 17), "evening": (17, 20)}


def parse_duration_minutes(text: str) -> int | None:
    """Minutes from a natural phrase, or None when none is stated.
    '45 min'->45, '1 hour'/'an hour'->60, 'half hour'/'half-hour'->30,
    '90m'->90, '1.5 hours'->90, '2 hrs'->120."""
    t = (text or "").lower()
    if re.search(r"\bhalf[\s-]?hour\b", t):
        return 30
    m = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)\b", t)
    if m:
        return max(5, min(int(round(float(m.group(1)) * 60)), 24 * 60))
    m = re.search(r"(\d+)\s*(minutes?|mins?|m)\b", t)
    if m:
        return max(5, min(int(m.group(1)), 24 * 60))
    if re.search(r"\ban?\s+hour\b", t):
        return 60
    return None


def interpret_constraints(
    text: str, now_local: datetime, *, default_minutes: int = 30
) -> dict:
    """Deterministic constraint table over the ASK TEXT ONLY (never the
    transcript — PII boundary). Returns a window (tz-aware, in now_local's
    zone), duration, working hours, preferred time-of-day, and whether a single
    day was targeted (so the ranker can honour a named weekend day)."""
    t = (text or "").lower()
    dur = parse_duration_minutes(t)
    duration_explicit = dur is not None
    duration = dur if dur is not None else default_minutes

    tz = now_local.tzinfo
    day0 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    single_day = False
    has_anchor = True  # a concrete date anchor was named (today/next week/…)
    start, end = now_local, now_local + timedelta(days=7)  # default: next 7 days

    if "today" in t:
        start, end, single_day = now_local, day0 + timedelta(days=1), True
    elif "tomorrow" in t:
        start = day0 + timedelta(days=1)
        end, single_day = start + timedelta(days=1), True
    elif "next week" in t:
        # next Monday 00:00 .. +7 days
        nm = day0 + timedelta(days=(7 - day0.weekday()) or 7)
        start, end = nm, nm + timedelta(days=7)
    elif "this week" in t:
        start = now_local
        end = day0 + timedelta(days=(7 - day0.weekday()))  # upcoming Monday
    else:
        has_anchor = False
        for name, wd in _WEEKDAYS.items():
            if re.search(rf"\b{name}\b", t):
                ahead = (wd - day0.weekday()) % 7
                ahead = ahead or 7  # "on monday" = next monday, not today
                start = day0 + timedelta(days=ahead)
                end, single_day, has_anchor = start + timedelta(days=1), True, True
                break

    preferred_tod = next((k for k in _TOD if k in t), None)
    return {
        "search_window_start": start,
        "search_window_end": end,
        "duration_minutes": duration,
        "duration_explicit": duration_explicit,
        "working_hours": (9, 18),
        "preferred_tod": preferred_tod,
        "single_day": single_day,
        "has_anchor": has_anchor,
        "timezone": str(getattr(tz, "key", "") or "UTC"),
    }


def _parse_dt(value: str):
    """RFC3339/ISO -> aware datetime, or None. Tolerates a trailing 'Z'."""
    s = (value or "").strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt


def rank_slots(
    busy: list[dict], *, window_start: datetime, window_end: datetime,
    duration_min: int, working_hours: tuple[int, int], preferred_tod=None,
    tz: str = "UTC", single_day: bool = False, max_candidates: int = 3,
) -> list[dict]:
    """Pure slot search. Walks a 15-min grid inside working hours, skips busy
    blocks (and weekends unless a single weekend day was targeted), ranks by
    preferred time-of-day then earliest, and returns up to ``max_candidates``
    slots as {slot_id, start, end, rank, conflicts} with NAIVE-LOCAL ISO
    times. Fully offline: ``busy`` is a list of {start,end} RFC3339 strings."""
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — unknown zone never crashes slot math
        zone = ZoneInfo("UTC")
    blocks = []
    for b in busy or []:
        bs, be = _parse_dt(b.get("start", "")), _parse_dt(b.get("end", ""))
        if bs and be and be > bs:
            blocks.append((bs, be))
    wh_start, wh_end = working_hours
    step = timedelta(minutes=15)
    dur = timedelta(minutes=max(5, duration_min))

    cands: list[tuple[int, datetime, datetime]] = []
    day = window_start.astimezone(zone).replace(
        hour=0, minute=0, second=0, microsecond=0)
    last = window_end.astimezone(zone)
    while day <= last and len(cands) < 400:
        weekend = day.weekday() >= 5
        if not (weekend and not single_day):
            cs = day.replace(hour=wh_start)
            day_end = day.replace(hour=wh_end)
            # align to the next :00/:15/:30/:45 and honour the window floor
            cs = max(cs, window_start.astimezone(zone))
            minute = (cs.minute + 14) // 15 * 15
            cs = cs.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
            while cs + dur <= min(day_end, window_end.astimezone(zone)):
                ce = cs + dur
                overlap = any(cs < be and bs < ce for bs, be in blocks)
                if not overlap:
                    tod_pen = 0
                    if preferred_tod and preferred_tod in _TOD:
                        lo, hi = _TOD[preferred_tod]
                        tod_pen = 0 if lo <= cs.hour < hi else 1
                    cands.append((tod_pen, cs, ce))
                cs += step
        day += timedelta(days=1)

    cands.sort(key=lambda c: (c[0], c[1]))
    out = []
    for rank, (_, cs, ce) in enumerate(cands[:max_candidates], start=1):
        s = cs.strftime(_LOCAL_FMT)
        out.append({
            "slot_id": hashlib.sha1(s.encode()).hexdigest()[:8],
            "start": s, "end": ce.strftime(_LOCAL_FMT),
            "rank": rank, "conflicts": [],
        })
    return out


def build_calendar_proposal(
    principal: str, *, oauth: dict | None = None, on_rotate=None,
    organizer_email: str = "", attendee_emails: list[str] | None = None,
    constraint_text: str = "", now: datetime | None = None,
    timezone: str = "UTC", default_minutes: int = 30, max_candidates: int = 3,
) -> dict:
    """Read free/busy over organizer + attendees and rank open slots into a
    CalendarProposal (the aura-v2 shape the approve doors consume). Never
    raises; empty candidate_slots when nothing fits or availability can't be
    read. Availability is read with ``principal`` (the ORG token — the calendar
    the event is actually booked on)."""
    from . import google_client

    try:
        zone = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001
        zone, timezone = ZoneInfo("UTC"), "UTC"
    now_local = now or datetime.now(zone)
    c = interpret_constraints(constraint_text, now_local, default_minutes=default_minutes)

    attendees = [e for e in (attendee_emails or []) if e]
    # 'primary' is the organizer's own calendar; attendee emails double as
    # calendar ids in Google's freeBusy.
    cal_ids = ["primary"] + attendees
    fb = google_client.freebusy(
        principal, cal_ids,
        c["search_window_start"].isoformat(), c["search_window_end"].isoformat(),
        oauth=oauth, on_rotate=on_rotate, timezone=timezone,
    )
    cals = (fb or {}).get("calendars") or {}

    busy_union: list[dict] = []
    for cid, info in cals.items():
        if (info or {}).get("status") == "readable":
            busy_union.extend(info.get("busy") or [])

    availability_sources = []
    for email in attendees:
        st = (cals.get(email) or {}).get("status") or "no_account"
        availability_sources.append({"attendee_email": email, "status": st})

    slots = rank_slots(
        busy_union,
        window_start=c["search_window_start"], window_end=c["search_window_end"],
        duration_min=c["duration_minutes"], working_hours=c["working_hours"],
        preferred_tod=c["preferred_tod"], tz=timezone,
        single_day=c["single_day"], max_candidates=max_candidates,
    )
    return {
        "candidate_slots": slots,
        "availability_sources": availability_sources,
        "selected_slot_id": "",
        "timezone": timezone,
        "search_window_start": c["search_window_start"].strftime(_LOCAL_FMT),
        "search_window_end": c["search_window_end"].strftime(_LOCAL_FMT),
        "duration_minutes": c["duration_minutes"],
        "working_hours": list(c["working_hours"]),
        "availability_scope": "all_attendees" if attendees else "organizer_only",
        "unavailable_attendees": [
            s["attendee_email"] for s in availability_sources
            if s["status"] != "readable"
        ],
    }


def _event_title(item: str) -> str:
    """A short human title for the calendar event derived from the ask."""
    t = re.sub(r"\s+", " ", (item or "").strip())
    t = re.sub(r"^(please\s+|can you\s+|could you\s+)", "", t, flags=re.IGNORECASE)
    return (t[:80] or "Follow-up meeting").strip()


def enrich_actions(
    actions: list, brief: str = "", *, principal: str,
    oauth: dict | None = None, on_rotate=None, organizer_email: str = "",
    attendees: list[str] | None = None, timezone: str = "UTC",
) -> list:
    """Finalize producer pass: for each UNTYPED action whose item is a vague
    scheduling ask (no concrete time), attach a typed calendar.create_event
    STUB (empty start/end) + a CalendarProposal. Attendee-less asks get no
    proposal (they stay untyped = today's behaviour). Best-effort: returns the
    actions unchanged on any error. Only runs when the flag calls it."""
    roster = [e for e in (attendees or []) if e]
    out = []
    for a in actions or []:
        if not isinstance(a, dict) or a.get("typed"):
            out.append(a)
            continue
        item = str(a.get("item") or a.get("step") or "")
        if not _SCHED_RE.search(item) or _ISOISH_RE.search(item):
            out.append(a)
            continue
        # Resolve attendees: emails named in the ask UNION the meeting roster —
        # never invented. A name-only ask ("with Ananth") resolves to nobody;
        # that's fine — we still propose the organizer's own free times.
        named = [e for e in _EMAIL_RE.findall(item)]
        who = list(dict.fromkeys([*named, *roster]))
        who = [e for e in who if e and e != organizer_email]
        # Concreteness gate: only propose when the ask carries SOME signal — an
        # attendee, an explicit duration, or a date anchor ("next week"). A bare
        # "we should sync sometime" has none, so it stays untyped (today's
        # behaviour) rather than dumping arbitrary free slots.
        try:
            _zone = ZoneInfo(timezone or "UTC")
        except Exception:  # noqa: BLE001
            _zone = ZoneInfo("UTC")
        c0 = interpret_constraints(item, datetime.now(_zone))
        if not (who or c0["duration_explicit"] or c0["has_anchor"]):
            out.append(a)
            continue
        try:
            proposal = build_calendar_proposal(
                principal, oauth=oauth, on_rotate=on_rotate,
                organizer_email=organizer_email, attendee_emails=who,
                constraint_text=item,
                default_minutes=30, timezone=timezone or "UTC",
            )
        except Exception:  # noqa: BLE001 — producer never breaks finalize
            out.append(a)
            continue
        out.append({
            **a,
            "typed": {"type": "calendar.create_event",
                      "args": {"title": _event_title(item), "start": "",
                               "end": "", "attendees": who}},
            "proposal": proposal,
        })
    return out
