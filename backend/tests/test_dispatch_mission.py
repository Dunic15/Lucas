"""The dashboard DISPATCH path forwards a per-meeting mission end to end.

A logged-in owner dispatching via POST /sessions/start with ``context.mission``
must land that mission in MeetingContext -> build_integration -> the session
integration dict (where ``cedric.resolve_mission`` reads it on the live path).

Key-free: sqlite in tmp_path, the Recall bot creation short-circuited, and no
orchestrator surface configured — so the mission is the ONLY thing that can make
the session carry an integration. An empty mission = today's request shape.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, store
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    # No Cedric surface: a mission-only dispatch must build its integration on
    # its own merit (guards against the environment making it non-None for us).
    monkeypatch.setattr(settings, "surface_webhook_url", "")
    monkeypatch.setattr(settings, "surface_context_url", "")
    return TestClient(main_module.app)


def _login(client: TestClient, email: str = "owner@x.com") -> dict:
    user = store.upsert_user(email)
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def _mock_dispatch(monkeypatch, captured: dict) -> None:
    """Capture the integration the endpoint hands to the vendor dispatch and
    short-circuit the real Recall bot creation + reconcile."""

    async def fake_start(meeting_url, avatar_id="", join_at=None,
                         integration=None, org_id=settings.demo_org_id):
        captured["integration"] = integration
        return {"bot_id": "b1", "conversation_id": "c1"}

    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main_module, "_meeting_has_active_bot", lambda url: False)
    monkeypatch.setattr(main_module, "_schedule_start_reconcile",
                        lambda *a, **k: None)
    monkeypatch.setattr(main_module, "_start_avatar_session", fake_start)


MISSION = "On this investor call, make sure market size is discussed."


def test_dispatch_forwards_mission_into_meeting_context(client, monkeypatch):
    _login(client)
    captured: dict = {}
    _mock_dispatch(monkeypatch, captured)

    r = client.post("/sessions/start", json={
        "meeting_url": "https://meet.google.com/mission-test",
        "avatar_id": "laura",
        "context": {"mission": MISSION},
    })
    assert r.status_code == 200, r.text
    integ = captured["integration"]
    assert integ is not None
    assert integ["mission"] == MISSION


def test_dispatch_without_mission_is_unchanged(client, monkeypatch):
    """No mission (and no other wiring) → no integration, exactly like today."""
    _login(client)
    captured: dict = {}
    _mock_dispatch(monkeypatch, captured)

    r = client.post("/sessions/start", json={
        "meeting_url": "https://meet.google.com/no-mission",
        "avatar_id": "laura",
    })
    assert r.status_code == 200, r.text
    assert captured["integration"] is None
