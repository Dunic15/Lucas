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
    # off to Cedric (deliver_ended) and does NOT run Laura's own execution.
    monkeypatch.setattr(settings, "surface_webhook_url", "https://meet-cedric.com/api/laura/events")
    monkeypatch.setattr(settings, "execute_enabled", True)
    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main_module.recall_client, "create_bot", lambda *a, **k: {"id": "bot_a"})
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda b: None)
    monkeypatch.setattr(main_module.anam_client, "end_conversation", lambda c: None)

    delivered, executed = [], []
    monkeypatch.setattr(cedric, "deliver_ended",
                        lambda integ, bot, art: delivered.append(bot) or True)
    monkeypatch.setattr(autopilot, "maybe_execute", lambda *a, **k: executed.append(a))

    # a bare start — no callback_url in the request
    client.post("/sessions/start", json={"meeting_url": "https://meet.google.com/mod-a-test", "avatar_id": "cedric"})
    store.get("bot_a").add_utterance("Ben", "Let's ship it.")
    assert client.post("/sessions/bot_a/end").status_code == 200
    time.sleep(0.2)
    assert delivered == ["bot_a"]   # handed off to Cedric
    assert executed == []           # Laura's own execution skipped (no double-act)


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
