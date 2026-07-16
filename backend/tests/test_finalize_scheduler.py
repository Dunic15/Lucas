"""The finalize find-a-time wire: with SCHEDULER_FIND_TIME on, a finalized
meeting whose action is a vague scheduling ask gets a CalendarProposal attached
(free/busy mocked). With the flag off, nothing is attached (prod default)."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import scheduler
from app.config import settings


def _mock_freebusy(monkeypatch):
    from app import google_client

    def fake(principal, cal_ids, ws, we, *, oauth=None, on_rotate=None, timezone="UTC"):
        return {"ok": True, "timezone": timezone,
                "calendars": {c: {"status": "readable", "busy": []} for c in cal_ids}}
    monkeypatch.setattr(google_client, "freebusy", fake)
    monkeypatch.setattr(google_client, "resolve_timezone", lambda *a, **k: "UTC")


def test_enrich_organizer_only_with_date_anchor(monkeypatch):
    """A name-only ask with a date anchor ("next week") — no resolvable email —
    still proposes the organizer's own free slots (organizer_only scope)."""
    _mock_freebusy(monkeypatch)
    actions = [{"action_id": "n1", "item": "book a follow-up with Ananth next week"}]
    out = scheduler.enrich_actions(actions, principal="org-1", timezone="UTC")
    a = out[0]
    assert a["typed"]["type"] == "calendar.create_event"
    assert a["proposal"]["availability_scope"] == "organizer_only"
    assert a["proposal"]["candidate_slots"], "organizer-only should still propose"
    assert a["typed"]["args"]["attendees"] == []  # name-only → no invitee resolved


def test_enrich_vague_no_signal_stays_untyped(monkeypatch):
    """No attendee, no duration, no date anchor => untyped (today's behaviour)."""
    _mock_freebusy(monkeypatch)
    out = scheduler.enrich_actions(
        [{"action_id": "n2", "item": "set up a sync sometime"}], principal="org-1")
    assert "typed" not in out[0] and "proposal" not in out[0]


def test_finalize_wire_present_and_flag_gated():
    """The finalize wire exists and is gated on settings.scheduler_find_time."""
    main_src = (Path(__file__).resolve().parents[1] / "app/main.py").read_text()
    assert "if settings.scheduler_find_time:" in main_src
    assert "scheduler.enrich_actions(" in main_src
    # default OFF so prod finalize is byte-identical until flipped
    assert settings.scheduler_find_time is False
