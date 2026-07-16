"""Calendar sight: the avatar knows the owner's upcoming Google Calendar.

Same contract as the Drive folder brief: assembled once at session start,
best-effort ("" when no Google is connected — the join proceeds), bounded
(it rides the live prompt), cached per org, content never logged. The
upcoming_meetings brain tool reads the session snapshot — zero network live.
"""
from __future__ import annotations

from types import SimpleNamespace

from app import google_client, tools


def _items():
    return [
        {
            "summary": "Weekly Planning",
            "start": {"dateTime": "2026-07-20T14:00:00+02:00"},
            "end": {"dateTime": "2026-07-20T14:30:00+02:00"},
            "attendees": [
                {"email": "owner@acme.com", "self": True},
                {"displayName": "Marco Rossi", "email": "marco@acme.com"},
                {"email": "priya@acme.com"},
            ],
        },
        {
            "summary": "Offsite",
            "start": {"date": "2026-07-22"},
            "end": {"date": "2026-07-23"},
        },
    ]


def test_brief_formats_time_title_guests(monkeypatch):
    google_client._brief_cache.clear()
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=8: {"ok": True, "events": _items()},
    )
    brief = google_client.calendar_brief("org-a")
    assert "Weekly Planning" in brief
    assert "14:00–14:30" in brief
    assert "Marco Rossi" in brief and "priya" in brief
    assert "owner" not in brief  # self is excluded
    assert "2026-07-22 (all day)" in brief


def test_brief_empty_when_not_connected(monkeypatch):
    google_client._brief_cache.clear()
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=8: {"ok": False, "error": "no oauth"},
    )
    assert google_client.calendar_brief("org-b") == ""


def test_brief_cached_per_org(monkeypatch):
    google_client._brief_cache.clear()
    calls = []

    def _fake(org, max_results=8):
        calls.append(org)
        return {"ok": True, "events": _items()}

    monkeypatch.setattr(google_client, "list_calendar_events", _fake)
    google_client.calendar_brief("org-c")
    google_client.calendar_brief("org-c")
    assert calls == ["org-c"]  # second hit served from cache


def test_brief_bounded(monkeypatch):
    google_client._brief_cache.clear()
    many = [
        {
            "summary": f"Very long meeting title that goes on and on {i}",
            "start": {"dateTime": "2026-07-20T14:00:00+02:00"},
        }
        for i in range(50)
    ]
    monkeypatch.setattr(
        google_client, "list_calendar_events",
        lambda org, max_results=8: {"ok": True, "events": many},
    )
    brief = google_client.calendar_brief("org-d")
    assert len(brief) <= google_client._BRIEF_MAX_CHARS
    assert len(brief.splitlines()) <= google_client._BRIEF_MAX_EVENTS


def test_brief_blank_org_is_empty():
    assert google_client.calendar_brief("") == ""


def test_upcoming_meetings_tool_reads_snapshot():
    session = SimpleNamespace(calendar_brief="- Mon 20 Jul 14:00 — Weekly Planning")
    out = tools.upcoming_meetings(session=session)
    assert "Weekly Planning" in out


def test_upcoming_meetings_tool_degrades_helpfully():
    assert "connect Google" in tools.upcoming_meetings(session=None)
    assert "connect Google" in tools.upcoming_meetings(
        session=SimpleNamespace(calendar_brief="")
    )


def test_tool_registered_for_brain_and_session():
    assert "upcoming_meetings" in tools._DISPATCH
    assert "upcoming_meetings" in tools._SESSION_TOOLS
    names = {
        t["function"]["name"]
        for t in tools.TOOL_SPECS
        if t.get("type") == "function"
    }
    assert "upcoming_meetings" in names  # the brain can actually see it
