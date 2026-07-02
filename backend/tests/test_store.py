"""Persistent store tests. No network calls or API keys."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    original = os.environ.get("LAURA_STORE_PATH")
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))

    import app.store as store  # noqa: WPS433

    store = importlib.reload(store)
    yield store

    if original is None:
        monkeypatch.delenv("LAURA_STORE_PATH", raising=False)
    else:
        monkeypatch.setenv("LAURA_STORE_PATH", original)
    importlib.reload(store)


def test_sessions_routes_artifacts_and_schedule_survive_reload(isolated_store):
    store = isolated_store
    session = store.create("bot_1", "https://meet.example/test", "laura")
    session.anam_conversation_id = "conv_1"
    session.proactive_done = True
    session.add_utterance("Alice", "Laura, what approval is needed?")
    session.mark_spoke()
    session.ws = object()

    store.register_conversation("conv_1", "bot_1")
    store.mark_scheduled("event_1")
    store.save_artifact("bot_1", {"summary": "done"})

    store = importlib.reload(store)

    loaded = store.get("bot_1")
    assert loaded is not None
    assert loaded.ws is None
    assert loaded.anam_conversation_id == "conv_1"
    assert loaded.proactive_done is True
    assert loaded.transcript_text() == "Alice: Laura, what approval is needed?"
    assert loaded.last_spoke_at > 0
    assert store.get_by_conversation("conv_1").bot_id == "bot_1"
    assert store.is_scheduled("event_1")
    assert store.get_artifact("bot_1") == {"summary": "done"}


def test_remove_deletes_live_session_but_keeps_artifact(isolated_store):
    store = isolated_store
    session = store.create("bot_1", "https://meet.example/test", "laura")
    session.anam_conversation_id = "conv_1"
    store.register_conversation("conv_1", "bot_1")
    store.save_artifact("bot_1", {"summary": "done"})

    store.remove("bot_1")
    store = importlib.reload(store)

    assert store.get("bot_1") is None
    assert store.get_by_conversation("conv_1") is None
    assert store.get_artifact("bot_1") == {"summary": "done"}
