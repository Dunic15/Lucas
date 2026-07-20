"""Drive folder listing for the Brain picker: when Google is connected the
dashboard lists the org's Drive folders (id+name) with its own token, so the
user PICKS a folder instead of pasting a link. Never raises — a missing
token/scope is a clean reason. Key-free (google_client + httpx stubbed)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import google_client
from app.knowledge import ingest


class _Resp:
    def __init__(self, status, files=None):
        self.status_code = status
        self._files = files or []

    def json(self):
        return {"files": self._files}


def test_not_connected_is_clean_reason(monkeypatch):
    monkeypatch.setattr(google_client, "_access_token", lambda org: ("", "no oauth"))
    folders, reason = ingest.list_folders("org1")
    assert folders == [] and "not connected" in reason  # no 500


def test_lists_folders_when_connected(monkeypatch):
    monkeypatch.setattr(google_client, "_access_token", lambda org: ("tok", ""))
    monkeypatch.setattr(ingest.httpx, "get", lambda *a, **k: _Resp(
        200, [{"id": "f1", "name": "Sales playbook"}, {"id": "f2", "name": "Ops"}]))
    folders, reason = ingest.list_folders("org1")
    assert reason == ""
    assert [f["name"] for f in folders] == ["Sales playbook", "Ops"]
    assert [f["id"] for f in folders] == ["f1", "f2"]


def test_scope_missing_maps_to_reconnect(monkeypatch):
    monkeypatch.setattr(google_client, "_access_token", lambda org: ("tok", ""))
    monkeypatch.setattr(ingest.httpx, "get", lambda *a, **k: _Resp(403))
    folders, reason = ingest.list_folders("org1")
    assert folders == [] and "scope missing" in reason


def test_transport_error_is_swallowed(monkeypatch):
    monkeypatch.setattr(google_client, "_access_token", lambda org: ("tok", ""))
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ingest.httpx, "get", boom)
    folders, reason = ingest.list_folders("org1")
    assert folders == [] and "drive list failed" in reason  # never raises
