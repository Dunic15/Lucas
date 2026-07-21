"""Drive connector: avatar.yaml drive_folder_id -> pre-meeting brief.

Key-free: the Drive API is faked at the httpx-client seam; recall/anam are
monkeypatched for the session-start test.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import avatars, drive_client, ledger, store

FOLDER = "1X_CD6ARfWskpNKZbWLtwaDWn7izGVKZH"


class _FakeResp:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@pytest.fixture(autouse=True)
def _fresh_cache():
    drive_client._cache.clear()
    yield
    drive_client._cache.clear()


def _fake_drive(monkeypatch, files, bodies, calls=None):
    """Fake the Drive HTTP seam: a listing + per-file export/download."""

    def fake_get(url, params=None, headers=None):
        if calls is not None:
            calls.append(url)
        if url.endswith("/files"):
            return _FakeResp(payload={"files": files})
        return _FakeResp(text=bodies.get(url.split("/files/")[1].split("/")[0], ""))

    monkeypatch.setattr(
        drive_client, "_client", type("C", (), {"get": staticmethod(fake_get)})
    )
    monkeypatch.setattr(drive_client, "_access_token", lambda: "tok")


def test_folder_brief_renders_docs_and_caches(monkeypatch):
    calls: list[str] = []
    _fake_drive(
        monkeypatch,
        files=[
            {"id": "d1", "name": "Project Apollo", "mimeType": "application/vnd.google-apps.document"},
            {"id": "d2", "name": "notes.md", "mimeType": "text/markdown"},
            {"id": "d3", "name": "deck.pptx", "mimeType": "application/vnd.ms-powerpoint"},
        ],
        bodies={"d1": "Pilot starts July 15.", "d2": "Owners: Marco, Elena."},
        calls=calls,
    )
    brief = drive_client.folder_brief(FOLDER)
    assert "## Project Apollo" in brief and "Pilot starts July 15." in brief
    assert "## notes.md" in brief and "Owners: Marco, Elena." in brief
    assert "deck.pptx" not in brief  # unsupported types skipped, never fetched

    n = len(calls)
    assert drive_client.folder_brief(FOLDER) == brief
    assert len(calls) == n  # served from cache — no second HTTP round


def test_folder_brief_truncates_at_cap(monkeypatch):
    _fake_drive(
        monkeypatch,
        files=[{"id": "big", "name": "wiki", "mimeType": "text/plain"}],
        bodies={"big": "x" * (drive_client.MAX_BRIEF_BYTES * 2)},
    )
    brief = drive_client.folder_brief(FOLDER)
    assert len(brief.encode()) <= drive_client.MAX_BRIEF_BYTES + 100
    assert brief.endswith("(folder brief truncated)")


def test_folder_brief_is_best_effort(monkeypatch):
    # No token (scope not granted yet) -> empty, no crash.
    monkeypatch.setattr(drive_client, "_access_token", lambda: "")
    assert drive_client.folder_brief(FOLDER) == ""
    drive_client._cache.clear()

    # API blowing up -> empty, no crash.
    def boom(*a, **k):
        raise RuntimeError("drive down")

    monkeypatch.setattr(drive_client, "_access_token", lambda: "tok")
    monkeypatch.setattr(
        drive_client, "_client", type("C", (), {"get": staticmethod(boom)})
    )
    assert drive_client.folder_brief(FOLDER) == ""
    # Unset folder is a no-op.
    assert drive_client.folder_brief("") == ""


def test_cedric_yaml_carries_the_folder():
    assert avatars.load("cedric").drive_folder_id == FOLDER
    assert avatars.load("laura").drive_folder_id == ""  # unaffected


def test_session_start_merges_drive_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    client = TestClient(main_module.app)

    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    made: list[int] = []

    def fake_create_bot(*a, **k):
        made.append(1)
        return {"id": f"bot_drive_{len(made)}"}

    monkeypatch.setattr(main_module.recall_client, "create_bot", fake_create_bot)
    monkeypatch.setattr(
        main_module.drive_client,
        "folder_brief",
        lambda folder_id, org_id="": "Pilot starts July 15." if folder_id == FOLDER else "",
    )

    resp = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/drv-tests-run", "avatar_id": "cedric"},
    )
    assert resp.status_code == 200, resp.text
    session = store.get("bot_drive_1")
    assert "Pilot starts July 15." in session.memory_brief
    assert "[Shared Drive folder" in session.memory_brief

    # An avatar with no folder never calls the connector: laura's brief clean.
    resp = client.post(
        "/sessions/start",
        json={"meeting_url": "https://meet.google.com/drv-plain-run", "avatar_id": "laura"},
    )
    assert resp.status_code == 200
    # bot id is the same stub; grab the latest session by meeting url
    plain = [s for s in store.all_sessions() if s.meeting_url.endswith("drv-plain-run")]
    assert plain and "[Shared Drive folder" not in (plain[0].memory_brief or "")
