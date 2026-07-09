"""Slack delivery in the Execution Pack — the interim meeting→Slack path that
doesn't depend on Cedric's Slack app. Posts the recap to SLACK_WEBHOOK_URL at
finalize. Key-free (Slack HTTP stubbed)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import actions, autopilot
from app.config import settings

ARTIFACT = {
    "summary": "Pilot planning sync.",
    "decisions": ["Go monthly at 49/seat"],
    "actions": [
        {"item": "Send rollout doc", "owner": "Marco", "deadline": "Friday"},
        {"item": "Send the recap", "owner": "UNASSIGNED", "requested_live": True},
    ],
}


def test_slack_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_email", False)
    monkeypatch.setattr(settings, "execute_drive_notes", False)
    monkeypatch.setattr(settings, "execute_calendar", False)
    monkeypatch.setattr(settings, "execute_slack", False)
    assert "slack" not in autopilot.maybe_execute("Cedric", ARTIFACT, "")


def test_slack_recap_formatting():
    text = autopilot._slack_recap("Cedric", ARTIFACT)
    assert "*Cedric — meeting recap*" in text
    assert "Pilot planning sync." in text
    assert "*Decisions*" in text and "Go monthly" in text
    assert "*Action items*" in text
    assert "Send rollout doc (Marco) — due Friday" in text
    # the live-asked action is flagged
    assert "asked in meeting" in text
    assert ":speech_balloon:" in text


def test_slack_posts_recap_at_execute(monkeypatch):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_email", False)
    monkeypatch.setattr(settings, "execute_drive_notes", False)
    monkeypatch.setattr(settings, "execute_calendar", False)
    monkeypatch.setattr(settings, "execute_slack", True)
    posted = []
    monkeypatch.setattr(actions, "post_to_slack", lambda text: posted.append(text) or {"sent": True})
    out = autopilot.maybe_execute("Cedric", ARTIFACT, "")
    assert out["slack"]["sent"] is True
    assert posted and "meeting recap" in posted[0]


def test_slack_no_webhook_is_clean(monkeypatch):
    # Real post_to_slack with no URL configured → clean {sent: False}, no raise.
    monkeypatch.setattr(settings, "slack_webhook_url", "")
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_recap_email", False)
    monkeypatch.setattr(settings, "execute_drive_notes", False)
    monkeypatch.setattr(settings, "execute_calendar", False)
    monkeypatch.setattr(settings, "execute_slack", True)
    out = autopilot.maybe_execute("Cedric", ARTIFACT, "")
    assert out["slack"]["sent"] is False and "SLACK_WEBHOOK_URL" in out["slack"]["reason"]
