"""Org-memory API (/org/*) — the read/act seam surfaces consume (issue #48).

Key-free like the rest of the suite: sqlite in tmp_path, no vendors touched.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import ledger, store
from app.config import settings

MEETING_URL = "https://meet.google.com/org-test-url"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


def _seed_ledger():
    """One finished meeting with an open action, via the public recorder."""
    ledger.record_meeting(
        MEETING_URL,
        "laura",
        "bot_org_1",
        {
            "summary": "Weekly sync happened.",
            "actions": [{"owner": "Marco", "item": "Send the rollout doc", "deadline": "Friday"}],
            "decisions": ["Ship v1 on Monday"],
        },
    )


def test_org_brief_returns_carryover(client):
    _seed_ledger()
    resp = client.get("/org/brief", params={"meeting_url": MEETING_URL})
    assert resp.status_code == 200
    data = resp.json()
    assert data["meeting_key"] == ledger.meeting_key(MEETING_URL)
    assert "rollout doc" in data["brief"]


def test_org_actions_groups_open_items(client):
    _seed_ledger()
    resp = client.get("/org/actions")
    assert resp.status_code == 200
    grouped = resp.json()["open"]
    key = ledger.meeting_key(MEETING_URL)
    assert key in grouped
    assert any("rollout doc" in i["item"] for i in grouped[key])


def test_org_resolve_closes_item(client):
    _seed_ledger()
    key = ledger.meeting_key(MEETING_URL)
    item_id = ledger.items(key, status="open")[0]["id"]

    resp = client.post(f"/org/actions/{item_id}/resolve")
    assert resp.status_code == 200 and resp.json()["resolved"] is True
    assert all(i["id"] != item_id for i in ledger.items(key, status="open"))
    # unknown / already-resolved id → clean 404
    assert client.post(f"/org/actions/{item_id}/resolve").status_code == 404


def test_org_endpoints_respect_bearer_gate(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sekrit")
    assert client.get("/org/brief", params={"meeting_url": MEETING_URL}).status_code == 401
    assert client.get("/org/actions").status_code == 401
    assert client.post("/org/actions/1/resolve").status_code == 401
    ok = client.get(
        "/org/actions", headers={"Authorization": "Bearer sekrit"}
    )
    assert ok.status_code == 200
