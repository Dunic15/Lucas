"""Every avatar has an email: plus-aliases of the watched inbox route invites.

laura.ai.122222+cedric@gmail.com  ->  cedric joins;  bare address -> default.
Covers the resolver, the calendar webhook path, and the Gmail watcher parsing.
Key-free like the rest of the suite.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import avatars, gmail_watcher, ledger, store
from app.config import settings

BASE = "laura.ai.122222@gmail.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


@pytest.fixture
def recall_stubbed(monkeypatch):
    created: list[dict] = []

    def fake_create_bot(meeting_url, avatar_page_url, join_at=None, bot_name="Laura"):
        created.append({"meeting_url": meeting_url, "bot_name": bot_name,
                        "avatar_page_url": avatar_page_url})
        return {"id": f"bot_{len(created)}"}

    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main_module.recall_client, "create_bot", fake_create_bot)
    monkeypatch.setattr(
        main_module.recall_client, "verify_webhook", lambda body, headers: None
    )
    return created


# ── the resolver ────────────────────────────────────────────────────────


def test_from_invite_email_resolves_known_tags():
    bases = [BASE]
    assert avatars.from_invite_email([f"laura.ai.122222+cedric@gmail.com"], bases) == "cedric"
    # case-insensitive, mixed with other attendees
    assert (
        avatars.from_invite_email(
            ["marco@example.com", "Laura.AI.122222+CEDRIC@Gmail.com"], bases
        )
        == "cedric"
    )
    # bare inbox address names nobody (caller falls back to the default)
    assert avatars.from_invite_email([BASE], bases) is None
    # unknown tag can never summon a ghost avatar
    assert avatars.from_invite_email(["laura.ai.122222+bob@gmail.com"], bases) is None
    # tag on a NON-watched base is ignored
    assert avatars.from_invite_email(["someone+cedric@example.com"], bases) is None
    assert avatars.from_invite_email([], bases) is None
    assert avatars.from_invite_email([f"laura.ai.122222+cedric@gmail.com"], []) is None


# ── calendar auto-join path ─────────────────────────────────────────────


def _calendar_event(attendee: str) -> dict:
    return {
        "attendees": [{"email": attendee}],
        "raw": {
            "start": {"dateTime": "2099-07-02T15:00:00+02:00"},
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/eml-rout-tst"}
                ]
            },
        },
    }


def _post_calendar(client, monkeypatch, attendee: str) -> dict:
    async def fake_events(payload):
        return [_calendar_event(attendee)]

    monkeypatch.setattr(main_module, "_calendar_events_from_payload", fake_events)
    resp = client.post("/webhooks/recall-calendar", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_calendar_plus_alias_summons_cedric(client, recall_stubbed, monkeypatch):
    monkeypatch.setattr(settings, "calendar_invite_emails", BASE)
    body = _post_calendar(client, monkeypatch, "laura.ai.122222+cedric@gmail.com")
    assert recall_stubbed, body  # the alias targets our inbox
    assert recall_stubbed[-1]["bot_name"] == "Cedric"
    assert "avatar_id=cedric" in recall_stubbed[-1]["avatar_page_url"]


def test_calendar_bare_address_stays_default(client, recall_stubbed, monkeypatch):
    monkeypatch.setattr(settings, "calendar_invite_emails", BASE)
    body = _post_calendar(client, monkeypatch, BASE)
    assert recall_stubbed, body
    assert recall_stubbed[-1]["bot_name"] == "Laura"


def test_calendar_unknown_tag_falls_back_to_default(client, recall_stubbed, monkeypatch):
    monkeypatch.setattr(settings, "calendar_invite_emails", BASE)
    body = _post_calendar(client, monkeypatch, "laura.ai.122222+nosuch@gmail.com")
    assert recall_stubbed, body  # still targets us (same inbox)…
    assert recall_stubbed[-1]["bot_name"] == "Laura"  # …but summons the default


def test_calendar_foreign_invite_still_skipped(client, recall_stubbed, monkeypatch):
    monkeypatch.setattr(settings, "calendar_invite_emails", BASE)
    body = _post_calendar(client, monkeypatch, "someone.else@example.com")
    assert not recall_stubbed
    assert body["scheduled"][0]["skipped"] == "invite_email_missing"


# ── gmail watcher parsing ───────────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_poll_new_invites_carries_recipient_addresses(monkeypatch):
    listing = {"messages": [{"id": "m1"}]}
    message = {
        "snippet": "Duccio is inviting you: https://meet.google.com/abc-defg-hij",
        "internalDate": "1783674000000",  # epoch ms, as Gmail returns it
        "payload": {
            "headers": [
                {"name": "To", "value": "Cedric <laura.ai.122222+cedric@gmail.com>"},
                {"name": "From", "value": "duccio@example.com"},
            ],
            "parts": [],
        },
    }

    def fake_get(url, **kwargs):
        return _FakeResp(message if "/messages/" in url else listing)

    monkeypatch.setattr(gmail_watcher, "_client", type("C", (), {"get": staticmethod(fake_get)}))
    seen: set[str] = set()
    out = gmail_watcher.poll_new_invites("tok", seen)
    assert len(out) == 1
    mid, url, addrs, received_at = out[0]
    assert mid == "m1" and url == "https://meet.google.com/abc-defg-hij"
    assert received_at == 1783674000.0  # seconds, for the boot-seeding cutoff
    assert "laura.ai.122222+cedric@gmail.com" in addrs
    # From (the sender) must NOT be treated as a recipient
    assert "duccio@example.com" not in addrs
    # …and the resolver turns it into the avatar id
    assert avatars.from_invite_email(addrs, [BASE]) == "cedric"


def test_poll_new_invites_missing_internal_date_is_zero(monkeypatch):
    """No internalDate → received_at 0.0: the boot-seeding pass treats it as
    old mail (never join); a normal post-seed pass still processes it."""
    listing = {"messages": [{"id": "m2"}]}
    message = {
        "snippet": "join https://meet.google.com/xyz-abcd-efg",
        "payload": {"headers": [], "parts": []},
    }

    def fake_get(url, **kwargs):
        return _FakeResp(message if "/messages/" in url else listing)

    monkeypatch.setattr(gmail_watcher, "_client", type("C", (), {"get": staticmethod(fake_get)}))
    out = gmail_watcher.poll_new_invites("tok", set())
    assert len(out) == 1 and out[0][3] == 0.0
