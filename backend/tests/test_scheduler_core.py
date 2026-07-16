"""Pure, offline tests for the find-a-time scheduler core: duration parsing,
constraint interpretation, slot ranking, and the finalize enrich pass (with
free/busy mocked). No network, no flag — these exercise the producer functions
directly."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import scheduler


# ── duration ──
def test_parse_duration():
    assert scheduler.parse_duration_minutes("book a 45 min sync") == 45
    assert scheduler.parse_duration_minutes("1 hour call") == 60
    assert scheduler.parse_duration_minutes("an hour") == 60
    assert scheduler.parse_duration_minutes("half-hour chat") == 30
    assert scheduler.parse_duration_minutes("90m") == 90
    assert scheduler.parse_duration_minutes("1.5 hours") == 90
    assert scheduler.parse_duration_minutes("catch up sometime") is None


# ── constraints ──
def test_next_week_window_starts_on_monday():
    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("UTC"))  # a Thursday
    c = scheduler.interpret_constraints("book 45 min with x next week", now)
    assert c["search_window_start"].weekday() == 0  # Monday
    assert c["search_window_start"] > now
    assert c["search_window_end"] - c["search_window_start"] == timedelta(days=7)
    assert c["duration_minutes"] == 45 and c["duration_explicit"] is True


def test_tomorrow_is_single_day():
    now = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("UTC"))
    c = scheduler.interpret_constraints("meet tomorrow afternoon", now)
    assert c["single_day"] is True
    assert c["preferred_tod"] == "afternoon"
    assert c["search_window_start"].date() == (now + timedelta(days=1)).date()


# ── ranking ──
def _mon_9to18():
    # a fixed Monday window 09:00–18:00 UTC
    start = datetime(2026, 7, 20, 9, 0, tzinfo=ZoneInfo("UTC"))
    return start, start.replace(hour=18)


def test_rank_avoids_busy_and_emits_naive_local():
    ws, we = _mon_9to18()
    busy = [{"start": "2026-07-20T09:00:00+00:00", "end": "2026-07-20T11:00:00+00:00"}]
    slots = scheduler.rank_slots(
        busy, window_start=ws, window_end=we, duration_min=45,
        working_hours=(9, 18), tz="UTC", single_day=True, max_candidates=3)
    assert slots, "expected at least one open slot"
    first = slots[0]
    # busy is 09–11, so the earliest free 45-min slot starts at 11:00
    assert first["start"] == "2026-07-20T11:00:00"
    assert first["end"] == "2026-07-20T11:45:00"
    assert "T" in first["start"] and "+" not in first["start"]  # naive-local
    assert first["slot_id"] and first["rank"] == 1


def test_rank_respects_preferred_afternoon():
    ws, we = _mon_9to18()
    slots = scheduler.rank_slots(
        [], window_start=ws, window_end=we, duration_min=30,
        working_hours=(9, 18), preferred_tod="afternoon", tz="UTC",
        single_day=True, max_candidates=1)
    assert slots and int(slots[0]["start"][11:13]) >= 12  # after noon


def test_rank_skips_weekend_on_multiday_window():
    # Fri 18:00 .. Mon: only Monday should yield slots (Sat/Sun skipped)
    ws = datetime(2026, 7, 17, 18, 0, tzinfo=ZoneInfo("UTC"))  # Friday evening
    we = datetime(2026, 7, 20, 18, 0, tzinfo=ZoneInfo("UTC"))  # Monday
    slots = scheduler.rank_slots(
        [], window_start=ws, window_end=we, duration_min=30,
        working_hours=(9, 18), tz="UTC", single_day=False, max_candidates=5)
    assert slots and all(s["start"].startswith("2026-07-20") for s in slots)


# ── enrich (finalize producer pass, freebusy mocked) ──
def _mock_freebusy(monkeypatch, *, ananth_busy=None):
    from app import google_client

    def fake_freebusy(principal, cal_ids, ws, we, *, oauth=None, on_rotate=None, timezone="UTC"):
        cals = {"primary": {"status": "readable", "busy": []}}
        for c in cal_ids:
            if "@" in c:
                cals[c] = {"status": "readable", "busy": ananth_busy or []}
        return {"ok": True, "timezone": timezone, "calendars": cals}

    monkeypatch.setattr(google_client, "freebusy", fake_freebusy)


def test_enrich_attaches_proposal_for_vague_ask(monkeypatch):
    _mock_freebusy(monkeypatch)
    actions = [{"action_id": "x1", "item": "book a 45 min with ananth@sffstudio.com next week"}]
    out = scheduler.enrich_actions(
        actions, principal="org-1", organizer_email="duccio@sffstudio.com",
        attendees=["ananth@sffstudio.com"], timezone="UTC")
    a = out[0]
    assert a["typed"]["type"] == "calendar.create_event"
    assert a["typed"]["args"]["start"] == "" and a["typed"]["args"]["end"] == ""
    assert "ananth@sffstudio.com" in a["typed"]["args"]["attendees"]
    prop = a["proposal"]
    assert prop["candidate_slots"], "expected candidate slots"
    assert prop["duration_minutes"] == 45
    assert {s["status"] for s in prop["availability_sources"]} == {"readable"}


def test_enrich_leaves_attendeeless_ask_untyped(monkeypatch):
    _mock_freebusy(monkeypatch)
    actions = [{"action_id": "x2", "item": "schedule a sync sometime"}]
    out = scheduler.enrich_actions(actions, principal="org-1")
    assert "typed" not in out[0] and "proposal" not in out[0]


def test_enrich_ignores_non_scheduling_actions(monkeypatch):
    _mock_freebusy(monkeypatch)
    actions = [{"action_id": "x3", "item": "send the deck to the team"}]
    out = scheduler.enrich_actions(
        actions, principal="org-1", attendees=["ananth@sffstudio.com"])
    assert "typed" not in out[0]
