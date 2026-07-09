"""Calendar follow-ups: the avatar books action deadlines as reminders — but
ONLY unambiguous dates (never guesses). Key-free; Google seam stubbed."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import autopilot, google_actions
from app.config import settings

# A Wednesday, for deterministic weekday math.
REF = date(2026, 7, 8)


# ── the conservative parser ──────────────────────────────────────────────


def test_parse_deadline_unambiguous():
    assert google_actions.parse_deadline("2026-07-15", REF) == date(2026, 7, 15)
    assert google_actions.parse_deadline("by Friday", REF) == date(2026, 7, 10)
    assert google_actions.parse_deadline("Fri", REF) == date(2026, 7, 10)
    assert google_actions.parse_deadline("tomorrow", REF) == date(2026, 7, 9)
    assert google_actions.parse_deadline("today", REF) == REF
    assert google_actions.parse_deadline("next week", REF) == date(2026, 7, 15)
    # same weekday as ref → today
    assert google_actions.parse_deadline("Wednesday", REF) == REF


def test_parse_deadline_returns_none_when_vague():
    for vague in ["", "soon", "end of quarter", "Q3", "ASAP", "later", "next sprint"]:
        assert google_actions.parse_deadline(vague, REF) is None


def test_parse_deadline_never_books_past_iso():
    # An ISO date already in the past is skipped, not booked.
    assert google_actions.parse_deadline("2020-01-01", REF) is None


# ── booking, gated + capped ──────────────────────────────────────────────

ARTIFACT = {
    "summary": "Sync.",
    "actions": [
        {"item": "Send rollout doc", "owner": "Marco", "deadline": "2026-07-10"},
        {"item": "Book review", "owner": "Ben", "deadline": "soon"},        # vague → skip
        {"item": "Prep slides", "owner": "Elena", "deadline": "Friday"},
    ],
    "decisions": ["x"],
}


def test_calendar_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_email", False)
    monkeypatch.setattr(settings, "execute_drive_notes", False)
    monkeypatch.setattr(settings, "execute_calendar", False)
    out = autopilot.maybe_execute("Cedric", ARTIFACT, "")
    assert "calendar" not in out


def test_calendar_books_only_parseable_deadlines(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_email", False)
    monkeypatch.setattr(settings, "execute_drive_notes", False)
    monkeypatch.setattr(settings, "execute_calendar", True)

    created = []
    monkeypatch.setattr(
        google_actions, "create_calendar_event",
        lambda summary, s, e, attendees=None: created.append((summary, s, e)) or {"created": True},
    )
    out = autopilot.maybe_execute("Cedric", ARTIFACT, "")
    assert out["calendar"]["booked"] == 2  # the ISO + the weekday; 'soon' skipped
    assert out["calendar"]["skipped"] == 1
    summaries = [c[0] for c in created]
    assert "[Cedric] Send rollout doc" in summaries
    assert "[Cedric] Prep slides" in summaries
    assert all("Book review" not in s for s in summaries)
    # events are 09:00 blocks on the due date
    assert any(c[1].startswith("2026-07-10T09:00") for c in created)


def test_calendar_capped(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_email", False)
    monkeypatch.setattr(settings, "execute_drive_notes", False)
    monkeypatch.setattr(settings, "execute_calendar", True)
    many = {"summary": "s", "decisions": ["d"], "actions": [
        {"item": f"Task {i}", "deadline": "2026-07-20"} for i in range(20)
    ]}
    n = []
    monkeypatch.setattr(google_actions, "create_calendar_event",
                        lambda *a, **k: n.append(1) or {"created": True})
    out = autopilot.maybe_execute("Cedric", many, "")
    assert out["calendar"]["booked"] == autopilot._MAX_CAL_EVENTS  # capped


def test_create_calendar_event_includes_timezone(monkeypatch):
    posted = {}

    class R:
        status_code = 200

        def json(self):
            return {"id": "ev1"}

    monkeypatch.setattr(google_actions, "_token", lambda: "tok")
    monkeypatch.setattr(settings, "execute_timezone", "Europe/Rome")
    monkeypatch.setattr(
        google_actions._client, "post",
        lambda url, headers=None, json=None: posted.update(json) or R(),
    )
    res = google_actions.create_calendar_event(
        "Test", "2026-07-10T09:00:00", "2026-07-10T09:30:00"
    )
    assert res["created"] is True
    assert posted["start"]["timeZone"] == "Europe/Rome"
    assert posted["end"]["timeZone"] == "Europe/Rome"
