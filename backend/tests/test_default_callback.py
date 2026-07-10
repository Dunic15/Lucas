"""Model A default routing: SURFACE_WEBHOOK_URL makes EVERY meeting (even an
email/API summon with no callback_url) hand off to Cedric — so Cedric does the
Slack posting + execution, and Laura's own autonomous execution is skipped.
Unset = today's autonomous behaviour (Model B). Key-free."""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import autopilot, cedric, ledger, store
from app.cedric import integration
from app.config import settings


class _Req:
    """Minimal StartRequest stand-in for build_integration."""
    def __init__(self, callback_url="", context_url="", external_ref=None, context=None):
        self.callback_url = callback_url
        self.context_url = context_url
        self.external_ref = external_ref
        self.context = context


def test_no_default_plain_start_stays_autonomous(monkeypatch):
    monkeypatch.setattr(settings, "surface_webhook_url", "")
    assert integration.build_integration(_Req(), "") is None  # Model B


def test_default_callback_routes_plain_start_to_cedric(monkeypatch):
    monkeypatch.setattr(settings, "surface_webhook_url", "https://meet-cedric.com/api/laura/events")
    integ = integration.build_integration(_Req(), "")
    assert integ is not None
    assert integ["callback_url"] == "https://meet-cedric.com/api/laura/events"


def test_explicit_callback_still_wins(monkeypatch):
    monkeypatch.setattr(settings, "surface_webhook_url", "https://default/events")
    integ = integration.build_integration(_Req(callback_url="https://explicit/cb"), "")
    assert integ["callback_url"] == "https://explicit/cb"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "s.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


def test_finalize_routes_to_cedric_and_skips_autonomous(client, monkeypatch):
    # Model A: with SURFACE_WEBHOOK_URL set, a plain email-style session hands
    # off to Cedric (deliver_ended) and does NOT also run Laura's own autopilot
    # delivery (orchestrated sessions belong to Cedric — no double-send).
    monkeypatch.setattr(settings, "surface_webhook_url", "https://meet-cedric.com/api/laura/events")
    monkeypatch.setattr(settings, "autopilot_deliver", True)
    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main_module.recall_client, "create_bot", lambda *a, **k: {"id": "bot_a"})
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda b: None)
    monkeypatch.setattr(main_module.anam_client, "end_conversation", lambda c: None)

    delivered, autopiloted = [], []
    monkeypatch.setattr(cedric, "deliver_ended",
                        lambda integ, bot, art: delivered.append(bot) or True)
    monkeypatch.setattr(autopilot, "maybe_deliver", lambda *a, **k: autopiloted.append(a))

    # a bare start — no callback_url in the request
    client.post("/sessions/start", json={"meeting_url": "https://meet.google.com/mod-a-test", "avatar_id": "cedric"})
    store.get("bot_a").add_utterance("Ben", "Let's ship it.")
    assert client.post("/sessions/bot_a/end").status_code == 200
    time.sleep(0.2)
    assert delivered == ["bot_a"]   # handed off to Cedric
    assert autopiloted == []        # Laura's own autopilot delivery skipped (no double-act)


def test_default_context_url_pulls_pre_meeting(monkeypatch):
    monkeypatch.setattr(settings, "surface_webhook_url", "")
    monkeypatch.setattr(settings, "surface_context_url", "https://meet-cedric.com/api/laura/context")
    integ = integration.build_integration(_Req(), "")
    assert integ is not None
    assert integ["context_url"] == "https://meet-cedric.com/api/laura/context"


def test_explicit_context_url_wins(monkeypatch):
    monkeypatch.setattr(settings, "surface_context_url", "https://default/context")
    integ = integration.build_integration(_Req(context_url="https://explicit/ctx"), "")
    assert integ["context_url"] == "https://explicit/ctx"


# ── default_integration(): the request-less SURFACE_* routing used by the
#    Gmail/calendar summon paths (which never build a StartRequest) ──


def test_default_integration_none_when_no_surface(monkeypatch):
    monkeypatch.setattr(settings, "surface_webhook_url", "")
    monkeypatch.setattr(settings, "surface_context_url", "")
    assert cedric.default_integration() is None  # stays autonomous / Model B


def test_default_integration_applies_webhook(monkeypatch):
    monkeypatch.setattr(settings, "surface_webhook_url", "https://meet-cedric.com/api/laura/events")
    monkeypatch.setattr(settings, "surface_context_url", "")
    integ = cedric.default_integration()
    assert integ is not None
    assert integ["callback_url"] == "https://meet-cedric.com/api/laura/events"
    assert integ["context_url"] == ""


def test_default_integration_context_only_no_callback(monkeypatch):
    # context URL set but no webhook: a valid mode (pre-meeting pull only), but
    # deliver_ended must return False so Model B delivery still runs.
    monkeypatch.setattr(settings, "surface_webhook_url", "")
    monkeypatch.setattr(settings, "surface_context_url", "https://meet-cedric.com/api/laura/context")
    integ = cedric.default_integration()
    assert integ is not None
    assert integ["callback_url"] == ""
    assert integ["context_url"] == "https://meet-cedric.com/api/laura/context"
    assert cedric.deliver_ended(integ, "bot_x", {"summary": "s"}) is False


def test_default_integration_keys_match_build_integration(monkeypatch):
    # Downstream consumers index these exact keys; keep parity with build_integration.
    monkeypatch.setattr(settings, "surface_webhook_url", "https://cb/events")
    built = integration.build_integration(_Req(), "")
    default = cedric.default_integration()
    assert set(default) == set(built)


def test_start_avatar_session_applies_default_integration(client, monkeypatch):
    # The gmail/calendar summon path calls _start_avatar_session(integration=None);
    # the default must be wired onto the session so Model A engages.
    import asyncio

    monkeypatch.setattr(settings, "surface_webhook_url", "https://meet-cedric.com/api/laura/events")
    monkeypatch.setattr(main_module.recall_client, "create_bot", lambda *a, **k: {"id": "bot_gmail"})
    monkeypatch.setattr(main_module.ledger, "carryover_brief", lambda url: "")
    monkeypatch.setattr(main_module.drive_client, "folder_brief", lambda fid: "")

    asyncio.run(main_module._start_avatar_session("https://meet.google.com/gmail-summon", "cedric"))
    s = store.get("bot_gmail")
    assert s.integration is not None
    assert s.integration["callback_url"] == "https://meet-cedric.com/api/laura/events"


# ── default external_ref (live finding 2026-07-10: {} → orchestrator can't
# route Slack cards/recaps, drops silently) ──


def test_default_external_ref_fills_empty(monkeypatch):
    monkeypatch.setattr(settings, "surface_webhook_url", "https://d/events")
    monkeypatch.setattr(
        settings, "surface_external_ref",
        '{"team": "T1", "slack_channel": "#cedric", "requested_by": "duccio"}',
    )
    integ = integration.build_integration(_Req(), "")
    assert integ["external_ref"] == {
        "team": "T1", "slack_channel": "#cedric", "requested_by": "duccio",
    }
    # request-less summons (gmail/calendar) get the same default
    integ2 = integration.default_integration()
    assert integ2["external_ref"]["slack_channel"] == "#cedric"


def test_session_own_external_ref_wins(monkeypatch):
    monkeypatch.setattr(settings, "surface_webhook_url", "https://d/events")
    monkeypatch.setattr(settings, "surface_external_ref", '{"team": "DEFAULT"}')
    integ = integration.build_integration(_Req(external_ref={"team": "REAL"}), "")
    assert integ["external_ref"] == {"team": "REAL"}


def test_invalid_default_external_ref_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "surface_webhook_url", "https://d/events")
    monkeypatch.setattr(settings, "surface_external_ref", "not-json{")
    assert integration.build_integration(_Req(), "")["external_ref"] == {}
    monkeypatch.setattr(settings, "surface_external_ref", '["not","a","dict"]')
    assert integration.default_integration()["external_ref"] == {}
