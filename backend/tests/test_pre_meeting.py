"""Pre-meeting context assembler — relevance, caps, budget, fail-open.

Deterministic section order, hard item/char caps, a wall-clock budget that
skips later sections, transcript exclusion + untrusted framing, and the
always-fail-open-to-empty guarantee (a Brain/DB failure never raises).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store
from app.config import settings
from app.meeting import meeting_memory, pre_meeting


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "s.sqlite3")
    monkeypatch.setattr(settings, "laura_database_url", "")
    monkeypatch.setattr(settings, "meeting_memory_enabled", True)
    monkeypatch.setattr(settings, "pre_meeting_context_enabled", True)
    meeting_memory._SQLITE_READY = False
    store._init_db()
    yield monkeypatch
    meeting_memory._SQLITE_READY = False


def _index(org, mid, summary, *, date=1.0):
    meeting_memory.index_artifact(org, mid, {
        "avatar_id": "laura", "summary": summary, "decisions": [],
        "actions": [], "visibility": "org", "principal_id": "u_runner",
        "transcript": "Sam: notes SECRETSAUCE-do-not-index",
    }, meeting_date=date)


def test_flag_off_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pre_meeting_context_enabled", False)
    assert pre_meeting.assemble("u_org", "laura",
                                participant_names=["Sam"]) == ""


def test_assembles_cited_sections_without_transcript(env):
    _index("u_org", "bot-1", "Postgres migration decided last week.")
    env.setattr(store, "list_decisions",
                lambda *a, **k: [{"decision": "Adopt Postgres",
                                  "status": "active"},
                                 {"decision": "old one", "status": "superseded"}])
    out = pre_meeting.assemble(
        "u_org", "laura",
        participant_names=["Sam", "Dana"],
        calendar_brief="Next: Standup 10am\nline2\nline3",
        title="Migration sync", principal_id="u_runner",
    )
    assert "untrusted" in out.lower()                 # framing
    assert "Participants: Sam, Dana" in out           # participants
    assert "Calendar: Next: Standup 10am" in out      # calendar metadata
    assert "Related past meetings:" in out and "id=bot-1" in out
    assert "Open decisions:" in out and "Adopt Postgres" in out
    assert "old one" not in out                       # superseded excluded
    assert "SECRETSAUCE-do-not-index" not in out      # never a transcript
    # Deterministic ordering: meeting → related → decisions.
    assert (out.index("Participants:") < out.index("Related past meetings:")
            < out.index("Open decisions:"))


def test_hard_item_cap(env):
    env.setattr(settings, "pre_meeting_context_max_items", 2)
    env.setattr(store, "list_decisions",
                lambda *a, **k: [{"decision": f"d{i}", "status": "active"}
                                 for i in range(10)])
    out = pre_meeting.assemble("u_org", "laura",
                               participant_names=["A", "B", "C"],
                               principal_id="u_runner")
    assert out.count("\n- ") <= 2                      # never exceeds the cap


def test_char_cap(env):
    # 200 is the assembler's floor; craft output well above it and assert the
    # hard truncation applies.
    env.setattr(settings, "pre_meeting_context_max_chars", 250)
    env.setattr(store, "list_decisions",
                lambda *a, **k: [{"decision": "x" * 500, "status": "active"}])
    out = pre_meeting.assemble("u_org", "laura",
                               participant_names=["Sam"], title="Big sync",
                               principal_id="u_runner")
    assert len(out) <= 250 + 2                          # truncated (+" …")
    assert out.endswith("…")


def test_budget_skips_later_sections(env):
    _index("u_org", "bot-1", "some prior meeting")
    env.setattr(store, "list_decisions",
                lambda *a, **k: [{"decision": "d", "status": "active"}])
    # A spent budget still emits the I/O-free section 1 but skips 2–4.
    out = pre_meeting.assemble("u_org", "laura",
                               participant_names=["Sam"],
                               principal_id="u_runner", budget_seconds=0.0)
    assert "Participants: Sam" in out
    assert "Related past meetings" not in out
    assert "Open decisions" not in out


def test_fail_open_on_section_error(env):
    def boom(*a, **k):
        raise RuntimeError("db down")

    env.setattr(meeting_memory, "search", boom)
    env.setattr(store, "list_decisions",
                lambda *a, **k: [{"decision": "survives", "status": "active"}])
    # The failing related-meetings section is dropped; the rest still renders.
    out = pre_meeting.assemble("u_org", "laura",
                               participant_names=["Sam"], principal_id="u_runner")
    assert "survives" in out
    assert "Related past meetings" not in out


def test_total_failure_returns_empty(env):
    def boom(*a, **k):
        raise RuntimeError("everything down")

    env.setattr(meeting_memory, "search", boom)
    env.setattr(store, "list_decisions", boom)
    # No participants, no calendar, and every read fails → "" (never raises).
    assert pre_meeting.assemble("u_org", "laura",
                                principal_id="u_runner") == ""


def test_empty_org_returns_empty(env):
    assert pre_meeting.assemble("", "laura", participant_names=["Sam"]) == ""
