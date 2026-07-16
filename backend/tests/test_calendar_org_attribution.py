"""Calendar auto-join must meter the org that OWNS the meeting, not demo_org.

The dispatch path is a webhook with no authenticated principal, so ownership
is resolved from the event itself: organizer first, then attendees, matched
against the org's connected Google account (org_oauth.email) or a registered
user (users.email). Unresolvable events keep the pre-fix Demo-org behavior,
and a personal u_<hash> match falls back too (it would fail the usage-gate
uuid cast and kill the join — strictly worse than demo attribution).
"""
from __future__ import annotations

import pytest

from app import control_plane, store
from app.config import settings
from app.main import (
    _calendar_event_organizer_email,
    _org_for_calendar_event,
)

ACME_ORG = "bf4a683b-1111-4222-8333-444455556666"  # durable uuid tenant


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Default: control plane off (key-free mode) — durability guard inactive.
    monkeypatch.setattr(control_plane, "enabled", lambda: False)
    # set_org_oauth encrypts at rest and fails closed without a key.
    monkeypatch.setattr(settings, "google_token_enc_key", "")
    monkeypatch.setattr(settings, "session_secret", "test-enc-secret")
    yield
    store.clear_org_oauth(ACME_ORG)


def _event(organizer="ceo@acme.com", attendees=("ceo@acme.com", "laura.ai.122222@gmail.com")):
    return {
        "raw": {
            "organizer": {"email": organizer},
            "attendees": [{"email": a} for a in attendees],
        }
    }


def test_org_for_email_prefers_connected_google(monkeypatch):
    assert store.set_org_oauth(ACME_ORG, "rt-x", email="CEO@Acme.com")
    assert store.org_for_email("ceo@acme.com") == ACME_ORG
    assert store.org_for_email("unknown@nowhere.dev") is None
    assert store.org_for_email("") is None


def test_organizer_email_extracted_from_raw():
    assert _calendar_event_organizer_email(_event()) == "ceo@acme.com"
    assert _calendar_event_organizer_email({}) == ""


def test_event_attributes_to_owning_org(monkeypatch):
    store.set_org_oauth(ACME_ORG, "rt-x", email="ceo@acme.com")
    assert _org_for_calendar_event(_event()) == ACME_ORG


def test_unresolvable_event_falls_back_to_demo(monkeypatch):
    monkeypatch.setattr(store, "org_for_email", lambda e: None)
    assert _org_for_calendar_event(_event()) == settings.demo_org_id


def test_avatar_own_inbox_never_attributes(monkeypatch):
    """The avatar's invite alias (incl. plus-tags) must not resolve ownership —
    only the humans on the event do."""
    calls = []

    def _spy(addr):
        calls.append(addr)
        return None

    monkeypatch.setattr(store, "org_for_email", _spy)
    monkeypatch.setattr(
        "app.main._calendar_target_emails",
        lambda: {"laura.ai.122222@gmail.com"},
    )
    ev = _event(
        organizer="laura.ai.122222+cedric@gmail.com",
        attendees=("laura.ai.122222@gmail.com", "human@acme.com"),
    )
    _org_for_calendar_event(ev)
    assert calls == ["human@acme.com"]


def test_personal_org_match_falls_back_when_control_plane_on(monkeypatch):
    """A u_<hash> personal-org match must not reach the entitlements gate."""
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(store, "org_for_email", lambda e: "u_3f9a1c")
    assert _org_for_calendar_event(_event()) == settings.demo_org_id


def test_durable_org_match_wins_when_control_plane_on(monkeypatch):
    monkeypatch.setattr(control_plane, "enabled", lambda: True)
    monkeypatch.setattr(store, "org_for_email", lambda e: ACME_ORG)
    assert _org_for_calendar_event(_event()) == ACME_ORG
