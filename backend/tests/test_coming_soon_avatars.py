"""Coming-soon avatars: listed, un-selectable, and refused by dispatch.

The refusal is the point. A disabled <option> stops a human; anyone holding an
API token calls /sessions/start directly, so the gate has to live server-side.

Key-free like the rest of the suite: recall/anam are monkeypatched, no vendor
is called.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import avatars, ledger, store
from app.config import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


@pytest.fixture
def recall_stubbed(monkeypatch):
    created: list[dict] = []

    def fake_create_bot(meeting_url, avatar_page_url, join_at=None, bot_name="Laura", avatar_id=""):
        created.append({"meeting_url": meeting_url, "avatar_id": avatar_id})
        return {"id": f"bot_{len(created)}"}

    monkeypatch.setattr(main_module.recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(main_module.recall_client, "create_bot", fake_create_bot)
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda bot_id: None)
    monkeypatch.setattr(main_module.recall_client, "delete_bot", lambda bot_id: None)
    monkeypatch.setattr(main_module.anam_client, "end_conversation", lambda conv_id: None)
    return created


def test_default_is_empty_so_no_avatar_is_hidden_by_accident():
    """The code default must not gate anyone.

    Defaulting this to "cedric" broke 38 tests across the clarify loop, the
    action bridge, the Cedric integration, voice consent and Drive — cedric is
    a genuinely dispatchable avatar here. Hiding one is a deployment's call,
    so the repo default stays empty and prod arms it.
    """
    assert settings.coming_soon_avatar_id_set == set()
    assert not avatars.is_coming_soon("cedric")


def test_flag_marks_only_the_named_avatar(monkeypatch):
    monkeypatch.setattr(settings, "coming_soon_avatar_ids", "cedric")
    assert avatars.is_coming_soon("cedric")
    assert avatars.is_coming_soon("  CEDRIC  ")  # trimmed + case-folded
    assert not avatars.is_coming_soon("laura")
    assert not avatars.is_coming_soon("petra")


def test_dispatch_is_refused_with_not_yet_not_not_found(
    client, recall_stubbed, monkeypatch
):
    """409, not the internal persona's 404: the id is real and the caller is
    meant to know it exists — it just isn't bookable yet."""
    monkeypatch.setattr(settings, "coming_soon_avatar_ids", "cedric")
    r = client.post(
        "/sessions/start",
        json={
            "meeting_url": "https://meet.google.com/aaa-bbbb-ccc",
            "avatar_id": "cedric",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"] == "avatar_not_available_yet"
    # No bot was created — the refusal lands before anything costs money.
    assert recall_stubbed == []


def test_a_bookable_avatar_is_untouched(client, recall_stubbed, monkeypatch):
    monkeypatch.setattr(settings, "coming_soon_avatar_ids", "cedric")
    r = client.post(
        "/sessions/start",
        json={
            "meeting_url": "https://meet.google.com/ddd-eeee-fff",
            "avatar_id": "laura",
        },
    )
    assert r.status_code == 200
    assert len(recall_stubbed) == 1
