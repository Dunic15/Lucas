"""Recall capacity errors are product-safe and actionable. No vendor calls."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import recall_client


def test_create_bot_maps_recall_507_to_avatar_busy(monkeypatch):
    monkeypatch.setattr(recall_client, "_headers", lambda: {"Authorization": "test"})
    # retry seam: exhaust the busy-retry budget instantly (no real waits)
    _sleeps = []
    monkeypatch.setattr(recall_client, "_BUSY_SLEEP", _sleeps.append)

    def fake_request(method, url, **kwargs):
        request = httpx.Request(method, url)
        return httpx.Response(507, json={"detail": "vendor-internal"}, request=request)

    monkeypatch.setattr(recall_client, "_request", fake_request)

    try:
        recall_client.create_bot(
            "https://meet.google.com/abc-defg-hij", "https://example.test/avatar"
        )
    except recall_client.AvatarBusyError as exc:
        assert str(exc) == "All avatars are busy right now — retry in a minute."
        assert "vendor-internal" not in str(exc)
    else:
        raise AssertionError("Recall 507 was not mapped to AvatarBusyError")
    # the transient-507 retry ran its full budget before giving up
    assert len(_sleeps) == recall_client._BUSY_RETRIES


def test_create_bot_busy_retry_recovers_when_capacity_frees(monkeypatch):
    """2026-07-24: Recall's shared avatar pool 507'd twice mid-demo with zero
    of our bots active, then cleared within minutes. A transient 507 must
    retry with backoff and succeed — never bounce the demo."""
    monkeypatch.setattr(recall_client, "_headers", lambda: {"Authorization": "test"})
    waits = []
    monkeypatch.setattr(recall_client, "_BUSY_SLEEP", waits.append)
    calls = {"n": 0}

    def fake_request(method, url, **kwargs):
        request = httpx.Request(method, url)
        calls["n"] += 1
        if calls["n"] <= 2:  # two rounds of full capacity exhaustion
            return httpx.Response(507, json={"detail": "busy"}, request=request)
        return httpx.Response(201, json={"id": "bot-777"}, request=request)

    monkeypatch.setattr(recall_client, "_request", fake_request)
    result = recall_client.create_bot(
        "https://meet.google.com/abc-defg-hij", "https://example.test/avatar"
    )
    assert result["id"] == "bot-777"
    assert waits == [10, 15]  # backoff schedule, then success


def test_sessions_start_returns_friendly_avatar_busy_payload(monkeypatch):
    monkeypatch.setattr(recall_client, "assert_ready", lambda: None)

    async def busy(*args, **kwargs):
        raise recall_client.AvatarBusyError("raw error must not escape")

    monkeypatch.setattr(main_module, "_start_avatar_session", busy)
    # Do not enter TestClient's lifespan context here: its shutdown deliberately
    # sets main._shutting_down for the process, which would pollute later
    # reconciler tests in a full-suite run.
    client = TestClient(main_module.app)
    response = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/abc-defg-hij"},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.json() == {
        "error": "avatar_busy",
        "detail": "All avatars are busy right now — retry in a minute.",
    }


def test_dashboard_prefers_friendly_avatar_busy_detail():
    dashboard = (Path(__file__).resolve().parents[2] / "frontend/dashboard.html").read_text()
    assert 'res.j.error==="avatar_busy" ? res.j.detail' in dashboard
