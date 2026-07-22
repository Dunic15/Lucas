"""A body-less dashboard POST must not 400 as "invalid JSON body".

Live bug 2026-07-22: the dashboard fires POST /dashboard/knowledge/
sources/<id>/sync with NO payload (there is nothing to say), but the
catch-all parsed request.json() for every POST — so the whole "connect a
Drive folder" chain died at its last step with "invalid JSON body".
Empty body → {}; malformed or non-object JSON still 400s.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app.knowledge import router as kroute


def _wire(monkeypatch, jobs):
    monkeypatch.setattr(kroute, "enabled", lambda: True)
    monkeypatch.setattr(kroute, "_dash_org", lambda request: (None, "org1"))
    monkeypatch.setattr(
        kroute.dal, "get_source", lambda org, sid: {"id": sid, "kind": "drive"}
    )
    monkeypatch.setattr(
        kroute.dal,
        "enqueue_job",
        lambda org, sid, kind: jobs.append((org, sid, kind)),
    )


def test_bodyless_sync_post_queues_the_job(monkeypatch):
    jobs: list = []
    _wire(monkeypatch, jobs)
    client = TestClient(main_module.app)
    r = client.post("/dashboard/knowledge/sources/s1/sync")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "queued": "sync_drive"}
    assert jobs == [("org1", "s1", "sync_drive")]


def test_malformed_body_still_400s(monkeypatch):
    jobs: list = []
    _wire(monkeypatch, jobs)
    client = TestClient(main_module.app)
    r = client.post(
        "/dashboard/knowledge/sources/s1/sync",
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid JSON body"
    assert jobs == []


def test_non_object_json_body_400s(monkeypatch):
    jobs: list = []
    _wire(monkeypatch, jobs)
    client = TestClient(main_module.app)
    r = client.post(
        "/dashboard/knowledge/sources/s1/sync",
        content="[1,2,3]",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert jobs == []
