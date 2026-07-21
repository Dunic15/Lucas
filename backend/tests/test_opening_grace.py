"""Opening settle-in ("wait to be called"): after joining, the avatar stays
silent UNLESS directly addressed by name — no joiner greetings, no unprompted
room-open answers. With first_call_required (the default) ONLY being named once
activates her; in legacy mode (first_call_required=False) the window also
expires after settings.opening_grace_seconds. No keys, no model."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402


def _session(tmp_path, monkeypatch, bot_id="grace-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    return store.create(bot_id, "https://meet.google.com/abc-defg-hij", "cedric")


def _line(bot_id: str, speaker: str, text: str) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": w} for w in text.split()],
                "participant": {"name": speaker, "id": 1},
            },
        },
    }


def _join(bot_id: str, name: str, pid) -> dict:
    return {
        "event": "participant_events.join",
        "data": {"bot": {"id": bot_id}, "data": {"participant": {"id": pid, "name": name}}},
    }


def _post(payload: dict) -> dict:
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    resp = asyncio.run(main.recall_webhook(FakeRequest()))
    return json.loads(resp.body)


# ── the helper's own logic ──


def test_in_opening_grace_logic(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.memory_brief = ""
    # SOLO relaxation (owner ask 2026-07-21): one (or zero) humans present →
    # the meeting IS a conversation with her; grace never applies.
    assert main._in_opening_grace(s) is False
    # A second human makes it a room again → grace applies as before.
    s.add_utterance("Ben", "hi")
    s.add_utterance("Alice", "hello")
    assert main._in_opening_grace(s) is True  # fresh join, not addressed

    # being named ends it
    s.addressed_once = True
    assert main._in_opening_grace(s) is False

    # first-call activation (the default): time alone NEVER ends it — however
    # long the meeting runs, she waits to be named once
    s.addressed_once = False
    s.created_at -= settings.opening_grace_seconds + 1
    assert main._in_opening_grace(s) is True

    # legacy time-boxed mode: time elapsing ends it, even if never named
    monkeypatch.setattr(settings, "first_call_required", False)
    assert main._in_opening_grace(s) is False

    # 0 disables the feature entirely (revert to speaking from line one)
    s.created_at = main.time.time()
    monkeypatch.setattr(settings, "opening_grace_seconds", 0)
    assert main._in_opening_grace(s) is False


# ── live path: unaddressed lines are held silent during the grace ──


def test_unaddressed_line_is_silent_during_grace(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.memory_brief = ""
    monkeypatch.setattr(settings, "deference_seconds", 30.0)  # would hang if reached

    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    # Two humans present: grace applies (solo meetings skip it — see the
    # logic test above).
    _post(_join(s.bot_id, "Alice", 2))
    _post(_join(s.bot_id, "Marco", 3))
    body = _post(_line(s.bot_id, "Ben", "can everyone hear me okay?"))
    assert body.get("reason") == "opening grace"
    assert body.get("spoke") is False
    assert not spoken  # she stayed quiet while the room settled
    assert s.addressed_once is False  # never named → still in grace
    store.remove(s.bot_id)


def test_being_named_ends_grace(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.memory_brief = ""
    monkeypatch.setattr(settings, "deference_seconds", 30.0)

    def instant_answer(*a, **k):
        yield "On the agenda: onboarding and pricing."

    monkeypatch.setattr(main, "answer_question_stream", instant_answer)

    async def fake_speak(session, line, citations=None, **kw):
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    body = _post(_line(s.bot_id, "Ben", "Cedric, what's on the agenda?"))
    assert body.get("reason") != "opening grace"  # a direct address always lands
    assert s.addressed_once is True  # …and wakes her for the rest of the meeting
    assert main._in_opening_grace(s) is False
    store.remove(s.bot_id)


def test_grace_expires_by_time_then_answers(tmp_path, monkeypatch):
    """Legacy time-boxed mode only — with first_call_required the grace never
    expires (covered in test_in_opening_grace_logic)."""
    s = _session(tmp_path, monkeypatch)
    s.memory_brief = ""
    monkeypatch.setattr(settings, "first_call_required", False)
    s.created_at -= settings.opening_grace_seconds + 1  # room has settled
    monkeypatch.setattr(settings, "deference_seconds", 0)  # no wait, deterministic

    def instant_answer(*a, **k):
        yield "The deadline is Friday."

    monkeypatch.setattr(main, "answer_question_stream", instant_answer)

    async def fake_speak(session, line, citations=None, **kw):
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    body = _post(_line(s.bot_id, "Ben", "when is the deadline again?"))
    assert body.get("reason") != "opening grace"  # grace is over → proactive again
    store.remove(s.bot_id)


# ── joiner greeting is suppressed during the grace ──


def test_joiner_greeting_suppressed_during_grace(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    # enough lines that the greeting's own len>=4 gate is satisfied — so the ONLY
    # thing holding the greeting back is the opening grace.
    for i in range(4):
        s.add_utterance("Ben", f"opening remark number {i}")

    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    _post(_join(s.bot_id, "Anna", 7))
    assert not spoken  # no "welcome Anna" over the still-settling room

    # once she's been activated (named once), the same joiner IS greeted
    s.addressed_once = True
    _post(_join(s.bot_id, "Priya", 8))
    assert spoken and "Priya" in spoken[0]
    store.remove(s.bot_id)
