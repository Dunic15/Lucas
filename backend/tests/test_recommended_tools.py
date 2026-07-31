"""Per-avatar recommended tools (dashboard card chips).

avatar.yaml declares `recommended_tools`; avatars.py normalizes them;
/dashboard/summary enriches each entry with org connect state + the connect
door the frontend renders. Key-free like the rest of the suite: no vendors are
called — connect state comes from the (empty) tmp sqlite store.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import store
from app.avatars import _normalize_recommended_tools


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)


@pytest.fixture
def client():
    return TestClient(main_module.app)


# ── the normalizer ─────────────────────────────────────────────────────────

def test_normalize_absent_and_blank_yield_empty():
    assert _normalize_recommended_tools(None) == []
    assert _normalize_recommended_tools([]) == []


def test_normalize_keeps_valid_and_sorts_by_priority():
    out = _normalize_recommended_tools([
        {"key": "b_tool", "name": "B", "kind": "builtin", "priority": 5},
        {"key": "a_tool", "name": "A", "kind": "native-google", "priority": 1,
         "why": "reason", "soon": True},
    ])
    assert [t["key"] for t in out] == ["a_tool", "b_tool"]
    assert out[0] == {"key": "a_tool", "name": "A", "kind": "native-google",
                      "why": "reason", "priority": 1, "soon": True}
    # defaults: why "", priority 99→kept as given, soon False
    assert out[1]["soon"] is False and out[1]["why"] == ""


def test_normalize_drops_malformed():
    out = _normalize_recommended_tools([
        "not-a-dict",
        {"name": "No key", "kind": "builtin"},
        {"key": "nokind", "name": "X", "kind": "frobnicator"},
        {"key": "bad slug", "name": "X", "kind": "pipedream:"},
        {"key": "UPPER CASE", "name": "X", "kind": "builtin"},
        {"key": "ok", "name": "OK", "kind": "pipedream:notion",
         "priority": "not-an-int"},
    ])
    assert [t["key"] for t in out] == ["ok"]
    assert out[0]["priority"] == 99  # unparseable priority falls back


# ── summary enrichment: connect state + doors ──────────────────────────────

def _tools(body: dict, avatar_id: str) -> dict:
    row = next(a for a in body["avatars"] if a["id"] == avatar_id)
    return {t["key"]: t for t in row["recommended_tools"]}


def test_summary_carries_cedric_tools_with_doors(client):
    body = client.get("/dashboard/summary").json()
    tools = _tools(body, "cedric")
    # native-google, org not connected in the key-free store → href door
    cal = tools["google_calendar"]
    assert cal["connected"] is False
    assert cal["connect"] == {"type": "href", "url": "/oauth/google/connect"}
    # contacts became a real native-google tool in the PA buildout — it rides
    # the same Google connection/door as the calendar
    contacts = tools["contacts"]
    assert contacts["soon"] is False
    assert contacts["connected"] is False
    assert contacts["connect"] == {"type": "href", "url": "/oauth/google/connect"}
    # builtin + soon → connected true, no door
    reminders = tools["reminders"]
    assert reminders["soon"] is True
    assert reminders["connected"] is True
    assert reminders["connect"] is None
    # pipedream → connected unknown server-side (client overlays), slug door
    cly = tools["calendly"]
    assert cly["connected"] is None
    assert cly["connect"] == {"type": "pipedream", "slug": "calendly"}


def test_summary_laura_visible_with_asana_door(client):
    body = client.get("/dashboard/summary").json()
    ids = [a["id"] for a in body["avatars"]]
    assert "laura" in ids  # no longer hidden
    laura = next(a for a in body["avatars"] if a["id"] == "laura")
    assert laura["role"] == "AI project manager"
    tools = _tools(body, "laura")
    asana = tools["asana"]
    assert asana["connected"] is False
    assert asana["connect"] == {"type": "view", "view": "connections"}


def test_summary_avatar_without_recommended_tools_ships_empty_list(client):
    body = client.get("/dashboard/summary").json()
    petra = next(a for a in body["avatars"] if a["id"] == "petra")
    assert petra["recommended_tools"] == []


def test_summary_connected_google_flips_chip(client, monkeypatch):
    monkeypatch.setattr(store, "get_org_oauth", lambda *a, **k: {"refresh": "tok"})
    body = client.get("/dashboard/summary").json()
    cal = _tools(body, "cedric")["google_calendar"]
    assert cal["connected"] is True
    assert cal["connect"] is None
