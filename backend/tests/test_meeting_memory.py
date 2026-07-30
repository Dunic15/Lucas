"""Meeting Memory Slice 1, key-free half: flag-off inertness, PII stripping,
attendee merge, deterministic digest, and never-raise guarantees — no
Postgres, no keys, no network."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.memory import meeting_memory  # noqa: E402

SENTINEL = "UTTERANCE-SENTINEL-NEVER-STORED"


def _session(**kw):
    base = dict(
        org_id="00000000-0000-0000-0000-0000000000de",
        bot_id="bot-1",
        meeting_url="https://meet.google.com/abc-defg-hij",
        avatar_id="laura",
        participants={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── flag off ⇒ inert everywhere (key-free demo unchanged) ───────────────────

def test_disabled_by_default_and_all_surfaces_inert():
    assert settings.meeting_memory_enabled is False
    assert meeting_memory.enabled() is False
    # No engine, no DB, no crash — every surface returns its empty shape.
    assert meeting_memory.deposit(_session(), {"summary": "x"}) is False
    assert meeting_memory.week_brief("00000000-0000-0000-0000-0000000000de") == ""
    assert meeting_memory.cached_digest("00000000-0000-0000-0000-0000000000de") == ""


def test_flag_on_without_control_plane_is_still_inert(monkeypatch):
    monkeypatch.setattr(settings, "meeting_memory_enabled", True)
    monkeypatch.setattr(settings, "laura_database_url", "")
    assert meeting_memory.enabled() is False
    assert meeting_memory.deposit(_session(), {"summary": "x"}) is False
    assert meeting_memory.week_brief("00000000-0000-0000-0000-0000000000de") == ""


def test_never_raises_even_when_engine_explodes(monkeypatch):
    monkeypatch.setattr(settings, "meeting_memory_enabled", True)
    monkeypatch.setattr(meeting_memory, "enabled", lambda: True)
    monkeypatch.setattr(
        meeting_memory.control_plane, "is_durable_org", lambda _o: True
    )

    def _boom():
        raise RuntimeError("no engine in this test")

    monkeypatch.setattr(meeting_memory, "_engine", _boom)
    assert meeting_memory.deposit(_session(), {"summary": "x"}) is False
    assert meeting_memory.week_brief("00000000-0000-0000-0000-0000000000de") == ""
    assert meeting_memory.cached_digest("00000000-0000-0000-0000-0000000000de") == ""


# ── PII: the distiller strips transcript-derived fields ─────────────────────

def test_distill_actions_drops_evidence_keeps_distilled_fields():
    actions = [
        {
            "item": "Send the DPA",
            "owner": "Dana",
            "deadline": "Friday",
            "action_id": "abc123",
            "gap_type": "document",
            "evidence": f"Dana: {SENTINEL} I'll send the DPA",
        },
        "not-a-dict-is-skipped",
    ]
    out = meeting_memory._distill_actions(actions)
    assert out == [
        {
            "item": "Send the DPA",
            "owner": "Dana",
            "deadline": "Friday",
            "action_id": "abc123",
            "gap_type": "document",
        }
    ]
    assert SENTINEL not in str(out)


# ── attendee merge: roster + speakers, deduped, agents excluded ─────────────

def test_attendees_merge_roster_and_speakers():
    session = _session(
        participants={
            "p1": {"id": "p1", "name": "Dana Fox", "kind": "human", "here": True},
            "p2": {"id": "p2", "name": "Laura", "kind": "agent", "here": True},
            "p3": {"id": "p3", "name": "dana  fox", "kind": "human", "here": True},
        }
    )
    artifact = {
        "participation": [
            {"name": "Dana Fox", "lines": 12},
            {"name": "Guest 1", "lines": 3},
        ]
    }
    attendees = meeting_memory._attendees_of(session, artifact)
    keys = sorted(meeting_memory._norm_dn(a["display"]) for a in attendees)
    # Agent excluded; "Dana Fox"/"dana fox" collapse to one; transcript-only
    # speaker (Guest 1) still counts and is marked as having spoken.
    assert keys == ["dana fox", "guest 1"]
    by_key = {meeting_memory._norm_dn(a["display"]): a for a in attendees}
    assert by_key["dana fox"]["spoke"] is True
    assert by_key["guest 1"]["spoke"] is True
    assert all(a["resolution"] == "display_name" for a in attendees)


# ── deterministic digest path ───────────────────────────────────────────────

def _row(day: str, summary: str, decisions: str = "[]") -> dict:
    return {
        "bot_id": "b",
        "meeting_key": "k",
        "meeting_type": "standup",
        "summary": summary,
        "decisions_json": decisions,
        "actions_json": "[]",
        "ended_at": "",
        "day": day,
    }


def test_fallback_digest_is_dated_and_capped():
    rows = [
        _row("07-29", "Reviewed the Q3 launch plan.", '["Ship on Aug 15"]'),
        _row("07-28", "Long summary " + "x" * 500),
    ]
    out = meeting_memory._fallback_digest(rows, 200)
    assert out.startswith("- 07-29 [standup]: Reviewed the Q3 launch plan.")
    assert "Decided: Ship on Aug 15" in out
    assert len(out) <= 200


def test_rows_as_input_capped():
    rows = [_row("07-2%d" % (i % 9), "s" * 400) for i in range(40)]
    assert len(meeting_memory._rows_as_input(rows)) <= meeting_memory._DIGEST_INPUT_CHARS
