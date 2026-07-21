"""One-time self-introduction on join (settings.self_introduce_on_join).

First-call etiquette keeps the avatar a SILENT guest until someone says her
name, which on a first-time room (nobody knows to call her by name) leaves a
joined-but-mute avatar with no cue how to activate her. Shortly after she's in
the call she says ONE short intro line telling the room how to call her in, then
goes back to waiting to be addressed. Fires once, never if she was activated
first, never twice, and off the live hot path. No keys, no model.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402


def _session(tmp_path, monkeypatch, bot_id="intro-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    # These tests pin the ROOM behavior (grace + intro address a settling
    # group). Solo meetings skip opening grace entirely (owner ask 2026-07-21),
    # so give the fixture two humans to stay a valid group scenario.
    s.participant_event("Ben", 1, here=True)
    s.participant_event("Alice", 2, here=True)
    monkeypatch.setattr(settings, "deference_seconds", 0)  # no wait if reached
    return s


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


class _FakeRequest:
    headers: dict = {}

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def body(self) -> bytes:
        return json.dumps(self._payload).encode()


def _capture(monkeypatch) -> list:
    spoken: list = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    return spoken


def _mute_answers(monkeypatch) -> None:
    """The answer stream never yields — isolates the intro from any answer the
    unaddressed lines might otherwise trigger once she's active."""
    monkeypatch.setattr(main, "answer_question_stream", lambda *a, **k: iter(()))


def _drive(payloads: list, wait: float = 0.1) -> None:
    """Post each webhook payload in ONE event loop, then wait so the detached
    self-intro task (scheduled via create_task) actually runs."""

    async def scenario() -> None:
        for p in payloads:
            await main.recall_webhook(_FakeRequest(p))
        await asyncio.sleep(wait)

    asyncio.run(scenario())


# ── happy path: never addressed → exactly ONE intro after the delay ──


def test_intro_fires_once_when_never_addressed(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    monkeypatch.setattr(settings, "self_introduce_after_seconds", 0.0)
    _mute_answers(monkeypatch)
    spoken = _capture(monkeypatch)

    _drive(
        [
            _line(s.bot_id, "Ben", "can everyone hear me okay?"),
            _line(s.bot_id, "Anna", "yes all good on my side thanks"),
        ]
    )

    assert len(spoken) == 1  # exactly one intro, no double-fire across lines
    assert "Laura" in spoken[0]  # she named herself
    # The intro is the fixed template — no transcript content leaks into it.
    assert "hear me" not in spoken[0].lower()
    assert s.self_introduced is True
    assert s.addressed_once is False  # etiquette intact: still waiting to be named
    store.remove(s.bot_id)


def test_disabled_stays_silent(tmp_path, monkeypatch):
    """self_introduce_on_join False → exactly today's behaviour (silent)."""
    s = _session(tmp_path, monkeypatch, bot_id="intro-off")
    monkeypatch.setattr(settings, "self_introduce_on_join", False)
    monkeypatch.setattr(settings, "self_introduce_after_seconds", 0.0)
    _mute_answers(monkeypatch)
    spoken = _capture(monkeypatch)

    _drive([_line(s.bot_id, "Ben", "can everyone hear me okay?")])

    assert spoken == []
    assert s.self_introduced is False
    store.remove(s.bot_id)


# ── never fires if the meeting activated her first ──


def test_no_intro_if_already_addressed(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="intro-addressed")
    s.addressed_once = True  # room already named her before the first line we see
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    monkeypatch.setattr(settings, "self_introduce_after_seconds", 0.0)
    _mute_answers(monkeypatch)
    spoken = _capture(monkeypatch)

    _drive([_line(s.bot_id, "Ben", "so where were we?")])

    assert spoken == []  # already active → the intro is moot
    assert s.self_introduced is True  # marked done so we stop re-checking
    store.remove(s.bot_id)


def test_no_intro_if_she_already_spoke(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="intro-spoke")
    s.last_spoke_at = time.time()  # she already said something this session
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    monkeypatch.setattr(settings, "self_introduce_after_seconds", 0.0)
    _mute_answers(monkeypatch)
    spoken = _capture(monkeypatch)

    _drive([_line(s.bot_id, "Ben", "can everyone hear me okay?")])

    assert spoken == []
    assert s.self_introduced is True
    store.remove(s.bot_id)


def test_intro_dropped_if_activated_during_delay(tmp_path, monkeypatch):
    """Scheduled off the first line, but the room names her before the delay
    elapses → the intro is dropped rather than talking over the live exchange."""
    s = _session(tmp_path, monkeypatch, bot_id="intro-race")
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    monkeypatch.setattr(settings, "self_introduce_after_seconds", 0.05)
    _mute_answers(monkeypatch)
    spoken = _capture(monkeypatch)

    async def scenario() -> None:
        await main.recall_webhook(
            _FakeRequest(_line(s.bot_id, "Ben", "can everyone hear me okay?"))
        )
        assert s.self_introduced is True  # scheduled
        s.addressed_once = True  # she gets named while the intro is still pending
        await asyncio.sleep(0.15)  # > the 0.05s delay

    asyncio.run(scenario())

    assert spoken == []  # activated during the wait → intro suppressed
    store.remove(s.bot_id)


# ── the scheduler is a pure, non-blocking flag flip ──


# ── never introduces OVER a human: wait for an open floor ──


def test_intro_waits_for_open_floor_then_fires_once(tmp_path, monkeypatch):
    """Floor busy at the delay (a human is mid-utterance) → she does NOT barge in
    immediately; she re-polls and introduces at the FIRST natural pause, once."""
    s = _session(tmp_path, monkeypatch, bot_id="intro-busyfloor")
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    monkeypatch.setattr(settings, "self_introduce_after_seconds", 0.0)
    monkeypatch.setattr(settings, "self_introduce_max_wait_seconds", 5.0)
    monkeypatch.setattr(settings, "interject_min_pause_seconds", 1.0)
    monkeypatch.setattr(main, "_SELF_INTRO_RECHECK_SECONDS", 0.01)
    _mute_answers(monkeypatch)
    spoken = _capture(monkeypatch)

    async def scenario():
        await main.recall_webhook(
            _FakeRequest(
                _line(s.bot_id, "Ben", "and then the numbers went up sharply last quarter")
            )
        )
        s.last_human_partial_at = main.time.time()  # presenter mid-utterance
        await asyncio.sleep(0.08)
        assert spoken == []  # she did NOT talk over the human
        s.last_human_partial_at = main.time.time() - 10.0  # a natural pause opens
        await asyncio.sleep(0.08)  # next recheck sees the open floor

    asyncio.run(scenario())
    assert len(spoken) == 1  # introduced exactly once, at the pause
    assert "Laura" in spoken[0]
    store.remove(s.bot_id)


def test_intro_gives_up_if_floor_busy_past_cap(tmp_path, monkeypatch):
    """The floor stays busy past self_introduce_max_wait_seconds → the moment has
    passed, she gives up silently (never talks over, never barges in late)."""
    s = _session(tmp_path, monkeypatch, bot_id="intro-capbusy")
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    monkeypatch.setattr(settings, "self_introduce_after_seconds", 0.0)
    monkeypatch.setattr(settings, "self_introduce_max_wait_seconds", 0.05)
    monkeypatch.setattr(settings, "interject_min_pause_seconds", 1.0)
    monkeypatch.setattr(main, "_SELF_INTRO_RECHECK_SECONDS", 0.01)
    _mute_answers(monkeypatch)
    spoken = _capture(monkeypatch)

    async def scenario():
        await main.recall_webhook(
            _FakeRequest(_line(s.bot_id, "Ben", "so the roadmap for next half is packed"))
        )
        s.last_human_partial_at = main.time.time()  # stays recent through the cap
        await asyncio.sleep(0.2)  # well past the 0.05s cap

    asyncio.run(scenario())
    assert spoken == []  # never introduced
    store.remove(s.bot_id)


def test_intro_skipped_if_session_removed_during_wait(tmp_path, monkeypatch):
    """The meeting ends (session finalized/removed) while the intro is pending →
    don't speak into an orphaned session object."""
    s = _session(tmp_path, monkeypatch, bot_id="intro-removed")
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    monkeypatch.setattr(settings, "self_introduce_after_seconds", 0.05)
    _mute_answers(monkeypatch)
    spoken = _capture(monkeypatch)

    async def scenario():
        await main.recall_webhook(
            _FakeRequest(_line(s.bot_id, "Ben", "can everyone hear me okay?"))
        )
        assert s.self_introduced is True  # scheduled
        store.remove(s.bot_id)  # meeting ends before the intro fires
        await asyncio.sleep(0.12)  # > the 0.05s delay

    asyncio.run(scenario())
    assert spoken == []  # no speak into an orphaned session


def test_maybe_self_introduce_flips_flag_once(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="intro-sched")
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    monkeypatch.setattr(settings, "self_introduce_after_seconds", 0.0)
    spoken = _capture(monkeypatch)

    async def scenario():
        first = main.maybe_self_introduce(s)
        second = main.maybe_self_introduce(s)  # already scheduled
        await asyncio.sleep(0.05)  # let the single scheduled task run
        return first, second

    first, second = asyncio.run(scenario())
    assert first is True  # scheduled once
    assert second is False  # never re-scheduled
    assert s.self_introduced is True
    assert len(spoken) == 1  # exactly one intro spoken
    store.remove(s.bot_id)
