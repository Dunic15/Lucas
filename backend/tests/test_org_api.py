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
    """No body at all — the pre-outcome-contract client shape (Cedric today).
    Must keep behaving byte-identically: item closes as 'done'."""
    _seed_ledger()
    key = ledger.meeting_key(MEETING_URL)
    item_id = ledger.items(key, status="open")[0]["id"]

    resp = client.post(f"/org/actions/{item_id}/resolve")
    assert resp.status_code == 200 and resp.json()["resolved"] is True
    assert resp.json()["status"] == "done"  # response echoes the applied status
    assert all(i["id"] != item_id for i in ledger.items(key, status="open"))
    assert ledger.items(key, status="done")[0]["id"] == item_id
    # unknown / already-resolved id → clean 404
    assert client.post(f"/org/actions/{item_id}/resolve").status_code == 404


def test_org_resolve_empty_body_still_means_done(client):
    """An explicitly-empty JSON object is 'done' too (body absent OR empty)."""
    _seed_ledger()
    key = ledger.meeting_key(MEETING_URL)
    item_id = ledger.items(key, status="open")[0]["id"]
    resp = client.post(f"/org/actions/{item_id}/resolve", json={})
    assert resp.status_code == 200 and resp.json()["status"] == "done"
    assert ledger.items(key, status="done")[0]["id"] == item_id


def test_org_resolve_outcome_rejected_is_terminal(client):
    _seed_ledger()
    key = ledger.meeting_key(MEETING_URL)
    item_id = ledger.items(key, status="open")[0]["id"]

    resp = client.post(
        f"/org/actions/{item_id}/resolve",
        json={"outcome": "rejected", "detail": "owner declined in Slack"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"resolved": True, "id": str(item_id), "status": "rejected"}
    assert ledger.items(key, status="open") == []  # no longer open
    row = ledger.items(key, status="rejected")[0]
    assert row["id"] == item_id
    assert row["resolution_detail"] == "owner declined in Slack"
    assert row["resolved_at"] is not None
    # terminal exactly like done: a second resolve (any outcome) → 404
    assert client.post(f"/org/actions/{item_id}/resolve").status_code == 404
    assert (
        client.post(f"/org/actions/{item_id}/resolve", json={"outcome": "done"}).status_code
        == 404
    )


def test_org_resolve_outcome_failed_by_action_id(client):
    """The failed outcome, via the stable action_id ref (Cedric's natural key)."""
    ledger.record_meeting(
        MEETING_URL,
        "cedric",
        "bot_fail",
        {"actions": [{"item": "Send the contract", "owner": "Ben",
                      "action_id": "aid_fail01"}]},
    )
    key = ledger.meeting_key(MEETING_URL)
    resp = client.post(
        "/org/actions/aid_fail01/resolve",
        json={"outcome": "failed", "detail": "gmail auth expired"},
    )
    assert resp.status_code == 200 and resp.json()["status"] == "failed"
    row = next(i for i in ledger.items(key) if i["action_id"] == "aid_fail01")
    assert row["status"] == "failed"
    assert row["resolution_detail"] == "gmail auth expired"
    assert ledger.items(key, status="open") == []  # terminal — never reopens
    assert client.post("/org/actions/aid_fail01/resolve").status_code == 404


def test_org_resolve_invalid_outcome_400(client):
    _seed_ledger()
    key = ledger.meeting_key(MEETING_URL)
    item_id = ledger.items(key, status="open")[0]["id"]

    resp = client.post(f"/org/actions/{item_id}/resolve", json={"outcome": "exploded"})
    assert resp.status_code == 400
    assert "outcome" in resp.json()["error"]
    # nothing was applied — the item is still open
    assert ledger.items(key, status="open")[0]["id"] == item_id
    # malformed JSON with a non-empty body is a client error too
    resp = client.post(
        f"/org/actions/{item_id}/resolve",
        content=b"not json", headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert ledger.items(key, status="open")[0]["id"] == item_id


def test_org_resolve_detail_capped_at_300(client):
    _seed_ledger()
    key = ledger.meeting_key(MEETING_URL)
    item_id = ledger.items(key, status="open")[0]["id"]
    resp = client.post(
        f"/org/actions/{item_id}/resolve",
        json={"outcome": "failed", "detail": "x" * 1000},
    )
    assert resp.status_code == 200
    assert len(ledger.items(key, status="failed")[0]["resolution_detail"]) == 300


def test_org_resolve_by_action_id(client):
    """Cedric closes an action by its stable action_id — the id it holds from the
    live action.requested event and the session.ended artifact — without ever
    seeing the numeric ledger row id."""
    ledger.record_meeting(
        MEETING_URL,
        "cedric",
        "bot_aid",
        {"actions": [
            {"item": "Book a follow-up", "owner": "Ben", "deadline": "Fri",
             "action_id": "aid_abc123"}
        ]},
    )
    key = ledger.meeting_key(MEETING_URL)
    assert any(i["action_id"] == "aid_abc123" for i in ledger.items(key, status="open"))

    resp = client.post("/org/actions/aid_abc123/resolve")
    assert resp.status_code == 200 and resp.json()["resolved"] is True
    assert all(i["action_id"] != "aid_abc123" for i in ledger.items(key, status="open"))
    # already-resolved / unknown id → clean 404 (idempotent for the caller)
    assert client.post("/org/actions/aid_abc123/resolve").status_code == 404
    # the numeric-id path still works alongside it
    assert client.post("/org/actions/999999/resolve").status_code == 404


def test_org_endpoints_respect_bearer_gate(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sekrit")
    assert client.get("/org/brief", params={"meeting_url": MEETING_URL}).status_code == 401
    assert client.get("/org/actions").status_code == 401
    assert client.post("/org/actions/1/resolve").status_code == 401
    ok = client.get(
        "/org/actions", headers={"Authorization": "Bearer sekrit"}
    )
    assert ok.status_code == 200
