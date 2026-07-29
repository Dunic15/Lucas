"""Pre-speak silence gate (owner rule 2026-07-29): before a turn's first
line the room must have been quiet — no human partial in flight, no new human
transcript line — for the configured window; a room that never goes quiet
drops the line (or diverts the streamed answer to meeting chat). Key-free."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402


def _session(tmp_path, monkeypatch, bot_id="gate-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    s.addressed_once = True
    monkeypatch.setattr(settings, "deference_seconds", 0)
    monkeypatch.setattr(settings, "recall_api_key", "")
    return s


def _wait(session, gate, generation=None):
    return asyncio.run(
        main._wait_for_quiet(session, gate, generation=generation)
    )


# ── _wait_for_quiet unit behaviour ──


def test_zero_gate_is_disabled(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.last_human_partial_at = time.time()  # someone talking RIGHT now
    assert _wait(s, 0.0) is True  # 0 disables: immediate pass
    store.remove(s.bot_id)


def test_quiet_room_passes_immediately(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="gate-quiet")
    s.last_human_partial_at = time.time() - 10.0  # long quiet
    t0 = time.time()
    assert _wait(s, 0.3) is True
    assert time.time() - t0 < 0.25  # no sleep needed — already quiet long enough
    store.remove(s.bot_id)


def test_fresh_partial_makes_it_wait(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="gate-wait")
    s.last_human_partial_at = time.time()  # partial JUST landed
    t0 = time.time()
    assert _wait(s, 0.4) is True
    assert time.time() - t0 >= 0.35  # gate held until 0.4s of quiet accrued
    store.remove(s.bot_id)


def test_never_quiet_times_out_false(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="gate-timeout")
    monkeypatch.setattr(settings, "speak_silence_gate_max_wait", 0.3)
    s.last_human_partial_at = time.time()  # talking BEFORE the gate first checks

    async def run() -> bool:
        async def keep_talking():
            for _ in range(12):
                s.last_human_partial_at = time.time()
                await asyncio.sleep(0.05)

        talker = asyncio.create_task(keep_talking())
        ok = await main._wait_for_quiet(s, 1.0)
        talker.cancel()
        return ok

    assert asyncio.run(run()) is False  # room never opened: drop, never talk over
    store.remove(s.bot_id)


def test_generation_bump_aborts(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="gate-bump")
    s.last_human_partial_at = time.time()
    gen = s.speech_generation

    async def run() -> bool:
        async def bump():
            await asyncio.sleep(0.05)
            store.bump_speech_generation(s)

        bumper = asyncio.create_task(bump())
        ok = await main._wait_for_quiet(s, 5.0, generation=gen)
        await bumper
        return ok

    assert asyncio.run(run()) is False  # superseded turn never speaks
    store.remove(s.bot_id)


# ── gated _make_avatar_speak ──


def test_gated_line_dropped_when_room_stays_busy(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="gate-speak")
    monkeypatch.setattr(settings, "speak_silence_gate_max_wait", 0.2)
    s.last_human_partial_at = time.time()  # talking BEFORE the gate first checks

    async def run() -> bool:
        async def keep_talking():
            for _ in range(10):
                s.last_human_partial_at = time.time()
                await asyncio.sleep(0.05)

        talker = asyncio.create_task(keep_talking())
        spoke = await main._make_avatar_speak(
            s, "An unprompted remark.", force=True, gate=1.0
        )
        talker.cancel()
        return spoke

    assert asyncio.run(run()) is False
    assert not s.pending_messages  # nothing queued for the page either
    store.remove(s.bot_id)


def test_ungated_call_sites_unchanged(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="gate-none")
    s.last_human_partial_at = time.time()  # someone mid-utterance
    spoke = asyncio.run(main._make_avatar_speak(s, "Hello there.", force=True))
    assert spoke is True  # gate=None: today's behaviour, byte-for-byte
    store.remove(s.bot_id)


# ── streamed answer: floor never opens → divert to meeting chat ──


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


def _post(payload: dict) -> dict:
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    resp = asyncio.run(main.recall_webhook(FakeRequest()))
    return json.loads(resp.body)


def test_streamed_answer_diverts_to_chat_when_floor_stays_busy(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="gate-divert")
    monkeypatch.setattr(settings, "speak_silence_gate_called_seconds", 0.5)
    monkeypatch.setattr(settings, "speak_silence_gate_max_wait", 0.2)
    monkeypatch.setattr(settings, "ack_enabled", False)

    def stream(*a, **k):
        # A human partial lands while the answer generates: the floor is taken.
        s.last_human_partial_at = time.time() + 60  # stays "talking" for the test
        yield "The onboarding window is two weeks."

    monkeypatch.setattr(main, "answer_question_stream", stream)
    posted: list[str] = []
    # Patch the chat seam itself (a real Recall key would trip the webhook's
    # realtime-capability auth; the divert path calls this seam either way).
    monkeypatch.setattr(
        main, "_post_to_meeting_chat", lambda session, text: posted.append(text)
    )

    payload = _line(s.bot_id, "Ben", "Laura, how long is onboarding?")

    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    async def call() -> dict:
        resp = await main.recall_webhook(FakeRequest())
        # The chat post is a DETACHED task (off the live path); give it a beat
        # on the same loop before asyncio.run tears the loop down.
        for _ in range(20):
            if posted:
                break
            await asyncio.sleep(0.05)
        return json.loads(resp.body)

    body = asyncio.run(call())
    assert body.get("spoke") is False
    assert body.get("reason") == "floor busy"
    assert body.get("chat") is True
    assert posted and "onboarding window" in posted[0]
    store.remove(s.bot_id)
