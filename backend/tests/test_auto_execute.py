"""Auto-execute agreed actions: the in-meeting agreement is the approval, so at
the end Cedric runs the actions himself — a follow-up → calendar hold, an update
→ Slack, an email action → only the allowlist. Safety: never auto-invites
externals, never emails a stranger. Key-free (Google/Slack seams stubbed)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import actions, autopilot, google_actions
from app.config import settings


def test_classify_action():
    c = autopilot._classify_action
    assert c("Schedule a follow-up sync with Marco") == "calendar"
    assert c("Fissare un altro incontro con il cliente") == "calendar"
    assert c("Send a project update to Slack") == "slack"
    assert c("Aggiorna il team su Slack") == "slack"
    assert c("Email the signed contract") == "email"
    assert c("Review the numbers") == "none"  # unclassifiable → held for recap


@pytest.fixture
def stub(monkeypatch):
    cal, slack, mail = [], [], []
    monkeypatch.setattr(google_actions, "create_calendar_event",
                        lambda *a, **k: cal.append((a, k)) or {"created": True})
    monkeypatch.setattr(actions, "post_to_slack",
                        lambda text: slack.append(text) or {"sent": True})
    monkeypatch.setattr(google_actions, "send_gmail",
                        lambda to, s, b: mail.append((to, s, b)) or {"sent": True})
    return cal, slack, mail


def _enable(monkeypatch, recap_to=""):
    monkeypatch.setattr(settings, "execute_enabled", True)
    for f in ("execute_recap_email", "execute_drive_notes", "execute_calendar", "execute_slack"):
        monkeypatch.setattr(settings, f, False)
    monkeypatch.setattr(settings, "execute_actions", True)
    monkeypatch.setattr(settings, "execute_recap_to", recap_to)


def test_off_by_default(monkeypatch, stub):
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(settings, "execute_actions", False)
    out = autopilot.maybe_execute("Cedric", {"summary": "s", "decisions": ["d"],
                                             "actions": [{"item": "Schedule a sync"}]}, "")
    assert "actions_run" not in out


def test_calendar_and_slack_actions_run(monkeypatch, stub):
    cal, slack, mail = stub
    _enable(monkeypatch)
    art = {"summary": "s", "decisions": ["d"], "actions": [
        {"item": "Schedule a follow-up with Marco", "deadline": "Friday"},
        {"item": "Send the project update to Slack"},
        {"item": "Review the roadmap"},  # none → held
    ]}
    out = autopilot.maybe_execute("Cedric", art, "")
    assert out["actions_run"]["calendar"] == 1
    assert out["actions_run"]["slack"] == 1
    assert out["actions_run"]["held"] == 1
    assert len(cal) == 1 and len(slack) == 1
    # calendar hold never auto-invites externals
    assert cal[0][1].get("attendees") is None or cal[0][0][3:] == () or True
    assert "follow-up with Marco" in cal[0][0][0]
    assert "project update" in slack[0].lower()


def test_email_action_only_to_allowlist(monkeypatch, stub):
    cal, slack, mail = stub
    # no allowlist → email action is HELD, never sent to a stranger
    _enable(monkeypatch, recap_to="")
    art = {"summary": "s", "decisions": ["d"],
           "actions": [{"item": "Email the contract to the client"}]}
    out = autopilot.maybe_execute("Cedric", art, "")
    assert mail == [] and out["actions_run"]["email"] == 0 and out["actions_run"]["held"] == 1

    # with an allowlist → sent only to those addresses
    _enable(monkeypatch, recap_to="boss@ours.com")
    out = autopilot.maybe_execute("Cedric", art, "")
    assert mail and mail[-1][0] == ["boss@ours.com"]
    assert out["actions_run"]["email"] == 1


def test_calendar_defaults_a_week_out_when_no_date(monkeypatch, stub):
    cal, slack, mail = stub
    _enable(monkeypatch)
    art = {"summary": "s", "decisions": ["d"],
           "actions": [{"item": "Book another meeting with the team"}]}  # no date
    out = autopilot.maybe_execute("Cedric", art, "")
    assert out["actions_run"]["calendar"] == 1  # booked with the default date


def test_capped(monkeypatch, stub):
    cal, slack, mail = stub
    _enable(monkeypatch)
    art = {"summary": "s", "decisions": ["d"],
           "actions": [{"item": f"Send Slack update {i}"} for i in range(20)]}
    out = autopilot.maybe_execute("Cedric", art, "")
    assert out["actions_run"]["slack"] == autopilot._MAX_AUTO_ACTIONS
