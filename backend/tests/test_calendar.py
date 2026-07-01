"""Calendar auto-join guard tests. No API calls, no secrets needed."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.main import (  # noqa: E402
    _calendar_event_meeting_url,
    _calendar_event_start,
    _calendar_event_targets_avatar,
    _extract_invite_emails,
)


LAURA_EMAIL = "laura.ai.122222@gmail.com"


def test_extract_invite_emails_from_common_payload_shapes():
    event = {
        "attendees": [
            {"email": "Laura.AI.122222@gmail.com"},
            {"emailAddress": {"address": "person@example.com"}},
        ],
        "platform_data": {
            "participants": [{"mail": "other@example.com"}],
        },
    }

    assert _extract_invite_emails(event) == {
        LAURA_EMAIL,
        "person@example.com",
        "other@example.com",
    }


def test_calendar_event_requires_laura_invite(monkeypatch):
    monkeypatch.setattr(settings, "calendar_invite_emails", LAURA_EMAIL)

    assert _calendar_event_targets_avatar(
        {"attendees": [{"email": LAURA_EMAIL}]}
    )
    assert not _calendar_event_targets_avatar(
        {"attendees": [{"email": "someone@example.com"}]}
    )


def test_calendar_event_does_not_treat_organizer_as_invite(monkeypatch):
    monkeypatch.setattr(settings, "calendar_invite_emails", LAURA_EMAIL)

    assert not _calendar_event_targets_avatar(
        {
            "organizer": {"email": LAURA_EMAIL},
            "attendees": [{"email": "someone@example.com"}],
        }
    )


def test_empty_calendar_invite_filter_preserves_old_behavior(monkeypatch):
    monkeypatch.setattr(settings, "calendar_invite_emails", "")

    assert _calendar_event_targets_avatar({"attendees": []})


def test_google_raw_event_start_and_meeting_url_are_supported():
    event = {
        "raw": {
            "start": {"dateTime": "2026-07-02T15:00:00+02:00"},
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
                ]
            },
        }
    }

    assert _calendar_event_start(event) == "2026-07-02T15:00:00+02:00"
    assert _calendar_event_meeting_url(event) == "https://meet.google.com/abc-defg-hij"
