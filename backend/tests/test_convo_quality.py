"""Conversation quality: barge-in gating + repetition guard. No vendors/keys."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402


def _session(bot_id: str = "convo-bot") -> store.Session:
    s = store.Session(bot_id=bot_id, meeting_url="https://meet.example/x")
    object.__setattr__(s, "_persist_enabled", False)  # unit test: no DB writes
    return s


def _speak(session, text, **kw):
    return asyncio.run(main._make_avatar_speak(session, text, **kw))


def test_repetition_guard_suppresses_identical_line():
    s = _session()
    assert _speak(s, "The DPA still needs a signature.") is True
    assert _speak(s, "The DPA still needs a signature.") is False  # exact repeat
    assert _speak(s, "the dpa STILL needs a signature!!") is False  # normalized repeat
    assert _speak(s, "Go-live is set for August 4.") is True  # different line fine


def test_repetition_guard_expires(monkeypatch):
    s = _session()
    monkeypatch.setattr(settings, "repeat_suppress_seconds", 0.05)
    assert _speak(s, "Hello there team") is True
    time.sleep(0.06)
    assert _speak(s, "Hello there team") is True  # window elapsed — allowed again


def test_speaking_window_extends_with_queued_lines():
    s = _session()
    _speak(s, "one two three four five six seven eight")
    first = s.speaking_until
    assert first > time.time()
    _speak(s, "nine ten eleven twelve thirteen fourteen fifteen sixteen")
    assert s.speaking_until > first  # queued speech extends the window


def test_barge_in_only_while_speaking():
    s = _session()
    assert main._should_barge_in(s, "Laura", "Priya", "wait I have a question") is False
    _speak(s, "a fairly long sentence that keeps her talking for a while now")
    assert main._should_barge_in(s, "Laura", "Priya", "wait I have a question") is True


def test_barge_in_ignores_self_and_backchannel():
    s = _session()
    _speak(s, "a fairly long sentence that keeps her talking for a while now")
    # her own transcribed voice must never interrupt her
    assert main._should_barge_in(s, "Laura", "Laura", "and the next step is provisioning") is False
    # two-word backchannel shouldn't cut her off
    assert main._should_barge_in(s, "Laura", "Priya", "yeah right") is False
    # disabled flag wins
    settings_backup = settings.barge_in_enabled
    try:
        settings.barge_in_enabled = False
        assert main._should_barge_in(s, "Laura", "Priya", "wait stop for a second") is False
    finally:
        settings.barge_in_enabled = settings_backup


def test_stop_message_queued_and_window_reset():
    s = _session()
    _speak(s, "a fairly long sentence that keeps her talking for a while now")
    asyncio.run(main._make_avatar_stop(s))
    assert s.speaking_until == 0.0
    types = [m["type"] for m in s.pending_messages]
    assert types[-1] == "stop"


def test_force_bypasses_repetition_guard():
    """Re-asking Laura BY NAME must answer again, even verbatim (review #2)."""
    s = _session()
    assert _speak(s, "Security approval is still missing.") is True
    assert _speak(s, "Security approval is still missing.") is False
    assert _speak(s, "Security approval is still missing.", force=True) is True


def test_repair_line_suppression_is_visible():
    """The repair line suppressed as a repeat returns False so the webhook can
    report spoke=false instead of lying (review #2)."""
    s = _session()

    class _A:  # minimal avatar surface for the repair line
        name = "Laura"

    line = main._silent_answer_repair_line(_A())
    assert _speak(s, line) is True
    assert _speak(s, line) is False


def test_own_speech_helper():
    assert main._is_own_speech("Laura", "Laura") is True
    assert main._is_own_speech("Laura SFF Expert", "laura") is True  # Recall bot label
    assert main._is_own_speech("Laura", " LAURA ") is True
    assert main._is_own_speech("Laura", "Priya") is False
