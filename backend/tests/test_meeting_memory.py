"""Meeting Memory — key-free (SQLite) distillation, indexing, and search.

The raw-transcript-exclusion invariant, permission-safe default-deny search,
citation grounding, the finalize (save_artifact) hook, and flag-off inertness.
The Postgres RLS/permission half lives in test_meeting_memory_pg.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store
from app.config import settings
from app.meeting import meeting_memory

_TRANSCRIPT = (
    "Sam: can we provision ACME today SECRETSAUCE-do-not-index\n"
    "Laura: yes, after approval\n"
    "Dana: I'll own the follow-up"
)


def _artifact(**over) -> dict:
    art = {
        "avatar_id": "laura",
        "meeting_type": "Onboarding sync",
        "summary": "We onboarded ACME. Next steps assigned to the team.",
        "decisions": ["Approved ACME provisioning"],
        "actions": [{"action": "Provision ACME tenant", "owner": "Sam",
                     "deadline": "Friday"}],
        "visibility": "org",
        "principal_id": "u_runner",
        "transcript": _TRANSCRIPT,
    }
    art.update(over)
    return art


@pytest.fixture
def mm(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    monkeypatch.setattr(settings, "laura_database_url", "")  # SQLite path
    monkeypatch.setattr(settings, "meeting_memory_enabled", True)
    meeting_memory._SQLITE_READY = False
    store._init_db()
    yield meeting_memory
    meeting_memory._SQLITE_READY = False


# ── distillation never carries the transcript ───────────────────────────────

def test_distill_excludes_transcript(mm):
    record = mm.distill(_artifact(), "bot-1", meeting_date=1_700_000_000.0)
    assert "transcript" not in record
    # Participants are derived NAMES only (avatar excluded), no transcript line.
    assert record["participants"] == ["Sam", "Dana"]
    blob = str(record)
    assert "SECRETSAUCE-do-not-index" not in blob
    assert "can we provision ACME today" not in blob
    assert record["title"].startswith("Onboarding sync")


def test_index_and_search_with_citation(mm):
    mm.index_artifact("u_org", "bot-42", _artifact(),
                      meeting_date=1_700_000_000.0)
    results = mm.search("u_org", "ACME provisioning", principal_ref="u_runner")
    assert len(results) == 1
    hit = results[0]
    assert hit["meeting_id"] == "bot-42"
    cite = mm.citation(hit)
    assert "id=bot-42" in cite and "2023-11-14" in cite  # title/date/id cited
    # The rendered brain block frames the content as untrusted and cites it.
    block = mm.format_results(results)
    assert "UNTRUSTED" in block and "id=bot-42" in block


def test_raw_transcript_never_in_search_output(mm):
    mm.index_artifact("u_org", "bot-7", _artifact(), meeting_date=1.0)
    results = mm.search("u_org", "ACME", principal_ref="u_runner")
    blob = str(results) + mm.format_results(results)
    assert "SECRETSAUCE-do-not-index" not in blob
    assert "can we provision ACME today" not in blob


# ── permission-safe, default-deny ───────────────────────────────────────────

def test_org_visibility_open_to_any_principal(mm):
    mm.index_artifact("u_org", "bot-o", _artifact(visibility="org"),
                      meeting_date=1.0)
    # A stranger principal (and even no principal) sees an org-visible meeting.
    assert mm.search("u_org", "ACME", principal_ref="u_stranger")
    assert mm.search("u_org", "ACME", principal_ref="")


def test_participants_visibility_default_deny(mm):
    mm.index_artifact("u_org", "bot-p",
                      _artifact(visibility="participants",
                                principal_id="u_runner"),
                      meeting_date=1.0)
    # Only the runner sees a participants/private meeting — a stranger is denied
    # (a transcript display name is never treated as an identity).
    assert mm.search("u_org", "ACME", principal_ref="u_runner")
    assert mm.search("u_org", "ACME", principal_ref="u_stranger") == []
    assert mm.search("u_org", "ACME", principal_ref="") == []


def test_private_visibility_runner_only(mm):
    mm.index_artifact("u_org", "bot-x",
                      _artifact(visibility="private", principal_id="u_owner"),
                      meeting_date=1.0)
    assert mm.search("u_org", "ACME", principal_ref="u_owner")
    assert mm.search("u_org", "ACME", principal_ref="u_someone") == []


def test_cross_org_isolation_sqlite(mm):
    mm.index_artifact("u_orgA", "bot-a", _artifact(), meeting_date=1.0)
    mm.index_artifact("u_orgB", "bot-b",
                      _artifact(summary="Different tenant meeting"),
                      meeting_date=1.0)
    a = mm.search("u_orgA", "ACME", principal_ref="u_runner")
    assert [r["meeting_id"] for r in a] == ["bot-a"]


def test_empty_query_returns_recent_authorized(mm):
    mm.index_artifact("u_org", "bot-old", _artifact(visibility="org"),
                      meeting_date=100.0)
    mm.index_artifact("u_org", "bot-new",
                      _artifact(visibility="org", summary="Newer meeting"),
                      meeting_date=200.0)
    recent = mm.search("u_org", "", principal_ref="")
    assert [r["meeting_id"] for r in recent] == ["bot-new", "bot-old"]


# ── flag-off inertness + finalize hook ──────────────────────────────────────

def test_flag_off_is_inert(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "s.sqlite3")
    monkeypatch.setattr(settings, "laura_database_url", "")
    monkeypatch.setattr(settings, "meeting_memory_enabled", False)
    meeting_memory._SQLITE_READY = False
    store._init_db()
    assert meeting_memory.index_artifact("u_org", "b", _artifact()) is False
    assert meeting_memory.search("u_org", "ACME") == []


def test_save_artifact_hook_indexes_when_enabled(mm):
    # The finalize path (store.save_artifact) drives the index for a personal
    # (SQLite) org; searching then finds the meeting.
    store.save_artifact("bot-final", _artifact(), org_id="u_hookorg")
    results = mm.search("u_hookorg", "onboarded ACME", principal_ref="u_runner")
    assert [r["meeting_id"] for r in results] == ["bot-final"]


def test_save_artifact_hook_silent_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "s2.sqlite3")
    monkeypatch.setattr(settings, "laura_database_url", "")
    monkeypatch.setattr(settings, "meeting_memory_enabled", False)
    meeting_memory._SQLITE_READY = False
    store._init_db()
    store.save_artifact("bot-off", _artifact(), org_id="u_off")
    monkeypatch.setattr(settings, "meeting_memory_enabled", True)
    meeting_memory._SQLITE_READY = False
    # Nothing was indexed while the flag was off.
    assert meeting_memory.search("u_off", "ACME", principal_ref="u_runner") == []
