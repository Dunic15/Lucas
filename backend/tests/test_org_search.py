"""Ask-across-meetings: GET /org/search finds decisions/actions across every
meeting (the 'employee that remembers' query). Key-free."""
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


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


def _seed(url, actions, decisions, summary, bot_id):
    ledger.record_meeting(url, "cedric", bot_id, {
        "actions": actions, "decisions": decisions, "summary": summary,
    })
    store.save_artifact(bot_id, {
        "summary": summary, "decisions": decisions, "actions": actions,
        "meeting_type": "status_update",
    })


def test_search_finds_ledger_and_meeting_matches(client):
    _seed("https://meet.google.com/aaa-bbbb-ccc",
          [{"item": "Send the pricing proposal", "owner": "Elena", "deadline": "Fri"}],
          ["Go with monthly pricing at 49/seat"],
          "Discussed pricing for the pilot.", "bot_p")
    _seed("https://meet.google.com/ddd-eeee-fff",
          [{"item": "Book the security review", "owner": "Ben"}],
          ["Launch on Monday"],
          "Security and launch planning.", "bot_s")

    r = client.get("/org/search", params={"q": "pricing"})
    assert r.status_code == 200
    data = r.json()
    assert data["query"] == "pricing"
    # ledger: the action + the decision both mention pricing
    ledger_items = [m["item"] for m in data["ledger_matches"]]
    assert any("pricing proposal" in i for i in ledger_items)
    assert any("monthly pricing" in i for i in ledger_items)
    # meeting artifact snippet surfaces too
    assert data["meeting_matches"] and any(
        "pricing" in m["snippet"].lower() for m in data["meeting_matches"]
    )
    # unrelated meeting is not in a pricing search's ledger hits
    assert not any("security review" in i for i in ledger_items)


def test_search_owner_query(client):
    _seed("https://meet.google.com/ggg-hhhh-iii",
          [{"item": "Ship the docs", "owner": "Marco"}], [], "Docs meeting.", "bot_m")
    r = client.get("/org/search", params={"q": "Marco"})
    assert any(m["owner"] == "Marco" for m in r.json()["ledger_matches"])


def test_search_empty_query_400(client):
    assert client.get("/org/search", params={"q": "  "}).status_code == 400


def test_search_respects_bearer_gate(client, monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "sekrit")
    assert client.get("/org/search", params={"q": "x"}).status_code == 401
    assert client.get(
        "/org/search", params={"q": "x"}, headers={"Authorization": "Bearer sekrit"}
    ).status_code == 200


def test_search_no_results_is_clean(client):
    _seed("https://meet.google.com/jjj-kkkk-lll",
          [{"item": "Do a thing", "owner": "X"}], [], "A meeting.", "bot_x")
    data = client.get("/org/search", params={"q": "nonexistentterm"}).json()
    assert data["ledger_matches"] == [] and data["meeting_matches"] == []
