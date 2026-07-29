"""OpenClaw Chat — authorized cross-meeting recall.

Chat can answer across authorized historical meetings, not only the selected
one: distilled + cited related-meeting context, the selected meeting excluded,
permission-safe default-deny, no transcript, and byte-identical (empty) context
when Meeting Memory is off.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store
from app.config import settings
from app.meeting import meeting_memory
from app.openclaw import runtime


@pytest.fixture
def mm(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "s.sqlite3")
    monkeypatch.setattr(settings, "laura_database_url", "")
    monkeypatch.setattr(settings, "meeting_memory_enabled", True)
    meeting_memory._SQLITE_READY = False
    store._init_db()
    yield
    meeting_memory._SQLITE_READY = False


def _index(org, mid, summary, *, visibility="org", principal_id="u_runner",
           date=1.0, decisions=None):
    art = {
        "avatar_id": "laura", "summary": summary,
        "decisions": decisions or [], "actions": [],
        "visibility": visibility, "principal_id": principal_id,
        "transcript": "Sam: hi SECRETSAUCE-do-not-index",
    }
    meeting_memory.index_artifact(org, mid, art, meeting_date=date)


def test_historical_context_returns_cited_related_meetings(mm):
    _index("u_org", "bot-1", "We chose Postgres for the migration.",
           decisions=["Adopt Postgres"], date=100.0)
    _index("u_org", "bot-2", "Budget approved for Q3.", date=200.0)
    ctx = runtime._historical_meeting_context(
        "u_org", "Postgres migration", principal_ref="u_runner"
    )
    ids = [c["meeting_id"] for c in ctx]
    assert "bot-1" in ids
    assert all("id=" in c["citation"] for c in ctx)
    assert "SECRETSAUCE-do-not-index" not in str(ctx)  # never a transcript


def test_historical_context_excludes_selected_meeting(mm):
    _index("u_org", "bot-1", "Postgres migration plan", date=100.0)
    ctx = runtime._historical_meeting_context(
        "u_org", "Postgres", principal_ref="u_runner",
        exclude_meeting_id="bot-1",
    )
    assert [c["meeting_id"] for c in ctx] == []


def test_historical_context_permission_default_deny(mm):
    _index("u_org", "bot-p", "Secret Postgres plan",
           visibility="participants", principal_id="u_runner")
    assert runtime._historical_meeting_context(
        "u_org", "Postgres", principal_ref="u_runner"
    )
    assert runtime._historical_meeting_context(
        "u_org", "Postgres", principal_ref="u_stranger"
    ) == []


def test_historical_context_off_when_flag_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "s.sqlite3")
    monkeypatch.setattr(settings, "laura_database_url", "")
    monkeypatch.setattr(settings, "meeting_memory_enabled", False)
    meeting_memory._SQLITE_READY = False
    store._init_db()
    assert runtime._historical_meeting_context(
        "u_org", "anything", principal_ref="u_runner"
    ) == []
