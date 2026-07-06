"""Autopilot follow-ups: gates default off, deliver/brief/nudge behavior."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import autopilot, ledger  # noqa: E402
from app.config import settings  # noqa: E402

ARTIFACT = {
    "summary": "s",
    "actions": [{"item": "Book the security assessment", "owner": "Daniel", "deadline": "", "gap_type": "none"}],
    "decisions": [],
    "missing_steps": ["dpa_confirmation"],
    "meeting_type": "customer_onboarding",
    "follow_up_email": {"subject": "Follow-up", "body": "Open items…"},
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    import app.store as store_mod

    importlib.reload(store_mod)
    importlib.reload(ledger)
    yield
    monkeypatch.delenv("LAURA_STORE_PATH", raising=False)
    importlib.reload(store_mod)
    importlib.reload(ledger)


@pytest.fixture
def outbox(monkeypatch):
    """Capture outbound email/Slack instead of hitting vendors."""
    sent = {"emails": [], "slack": []}
    monkeypatch.setattr(
        autopilot.actions, "send_email",
        lambda to, subject, body: (sent["emails"].append((to, subject, body)) or {"sent": True}),
    )
    monkeypatch.setattr(
        autopilot.actions, "post_to_slack",
        lambda text: (sent["slack"].append(text) or {"sent": True}),
    )
    return sent


def test_everything_disabled_by_default(outbox):
    assert settings.autopilot_deliver is False
    assert settings.autopilot_brief is False
    assert settings.autopilot_nudge is False
    assert autopilot.maybe_deliver("Laura", ARTIFACT) == {"delivered": False, "reason": "disabled"}
    assert autopilot.maybe_send_brief("https://meet.google.com/x", "Laura")["sent"] is False
    assert autopilot.nudge_due() is False
    assert outbox["emails"] == [] and outbox["slack"] == []


def test_deliver_sends_email_and_slack(outbox, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_deliver", True)
    monkeypatch.setattr(settings, "autopilot_deliver_to", "owner@example.com, pm@example.com")
    r = autopilot.maybe_deliver("Laura", ARTIFACT)
    assert r["delivered"] is True and r["email"]["sent"] is True
    (to, subject, _body), = outbox["emails"]
    assert to == ["owner@example.com", "pm@example.com"] and subject == "Follow-up"
    assert outbox["slack"]  # artifact summary posted


def test_deliver_never_raises(outbox, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_deliver", True)
    monkeypatch.setattr(settings, "autopilot_deliver_to", "owner@example.com")
    monkeypatch.setattr(
        autopilot.actions, "send_email",
        lambda *a: (_ for _ in ()).throw(RuntimeError("vendor down")),
    )
    r = autopilot.maybe_deliver("Laura", ARTIFACT)
    assert r == {"delivered": False, "reason": "RuntimeError"}


def test_brief_skips_without_history_and_sends_with(outbox, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_brief", True)
    monkeypatch.setattr(settings, "autopilot_deliver_to", "owner@example.com")
    url = "https://meet.google.com/aut-brief-tst"
    assert autopilot.maybe_send_brief(url, "Laura")["sent"] is False  # no history

    ledger.record_meeting(url, "laura", "bot-1", ARTIFACT)
    r = autopilot.maybe_send_brief(url, "Laura")
    assert r["sent"] is True and r["email"]["sent"] is True
    (_to, subject, body), = outbox["emails"]
    assert "pre-meeting brief" in subject
    assert "DPA confirmation" in body and "Book the security assessment" in body


def test_nudge_digest_groups_open_items(monkeypatch):
    ledger.record_meeting("https://meet.google.com/nud-getst-aa", "laura", "b1", ARTIFACT)
    digest = autopilot.nudge_digest()
    assert "nud-getst-aa" in digest
    assert "[process step] DPA confirmation" in digest
    assert "[action] Book the security assessment (owner: Daniel)" in digest


def test_nudge_cadence(monkeypatch):
    monkeypatch.setattr(settings, "autopilot_nudge", True)
    monkeypatch.setattr(settings, "autopilot_nudge_hours", 1.0)
    autopilot._last_nudge["at"] = 0.0
    assert autopilot.nudge_due(now=10_000.0) is True
    autopilot._last_nudge["at"] = 10_000.0
    assert autopilot.nudge_due(now=10_000.0 + 3599) is False
    assert autopilot.nudge_due(now=10_000.0 + 3601) is True
