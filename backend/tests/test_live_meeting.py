"""Live-meeting webhook behaviour: wake-word gating, direct on-topic questions,
transcript-triggered barge-in (stop emission + stale-chunk drop), and the
repetition guard. Drives the real recall_webhook with a fake request + an
in-memory session; brain streaming is stubbed so no model/network is touched."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402


class _Req:
    """Minimal stand-in for a Starlette Request the webhook needs."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()
        self.headers: dict[str, str] = {}  # unsigned realtime transcript

    async def body(self) -> bytes:
        return self._body


def _transcript(bot_id: str, speaker: str, text: str) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": w} for w in text.split()],
                "participant": {"name": speaker, "id": speaker},
            },
        },
    }


def _post(bot_id: str, speaker: str, text: str) -> dict:
    resp = asyncio.run(main.recall_webhook(_Req(_transcript(bot_id, speaker, text))))
    return json.loads(bytes(resp.body))


@pytest.fixture
def session(monkeypatch):
    # Keep everything in memory: no SQLite writes, no FK constraints.
    monkeypatch.setattr(store, "_persist_session", lambda s: None)
    monkeypatch.setattr(store, "_persist_utterance", lambda *a, **k: None)
    monkeypatch.setattr(settings, "require_wake_word", True)
    monkeypatch.setattr(settings, "proactive_enabled", False)  # isolate the gate
    s = store.create("bot_live_test", "https://meet.test", "laura")
    yield s
    store._sessions.pop("bot_live_test", None)


def _stub_answer(monkeypatch, sentences):
    monkeypatch.setattr(main, "answer_question_stream", lambda *a, **k: iter(list(sentences)))


# ── wake-word gating in live mode ──
def test_live_requires_wake_word_for_generic_line(session, monkeypatch):
    _stub_answer(monkeypatch, ["should not be said"])
    out = _post(session.bot_id, "Bob", "I think we should ship on Friday")
    assert out["spoke"] is False
    assert out["reason"] == "not called"
    assert store.drain_avatar_messages(session) == []


def test_live_answers_when_called_by_name(session, monkeypatch):
    _stub_answer(monkeypatch, ["Managers approve access first."])
    out = _post(session.bot_id, "Bob", "Laura, what is the approval process?")
    assert out["spoke"] is True
    msgs = store.drain_avatar_messages(session)
    assert [m["text"] for m in msgs] == ["Managers approve access first."]


def test_live_answers_direct_on_topic_question_without_name(session, monkeypatch):
    _stub_answer(monkeypatch, ["A security lead must approve elevated access."])
    out = _post(session.bot_id, "Bob", "What approval step is required for provisioning?")
    assert out["spoke"] is True


def test_reported_speech_does_not_wake_in_live_mode(session, monkeypatch):
    _stub_answer(monkeypatch, ["should not be said"])
    out = _post(session.bot_id, "Bob", "As Laura said, we should move on.")
    assert out["spoke"] is False
    assert out["reason"] == "not called"


# ── transcript-triggered barge-in ──
def test_barge_in_emits_stop_and_marks_interrupted(session, monkeypatch):
    # Simulate the avatar being mid-speech: a live turn began and just emitted audio.
    gen = session.begin_speaking()
    session.mark_spoke()
    # A queued (not-yet-delivered) speak chunk from that turn should be cleared.
    store.queue_avatar_message(session, {"type": "speak", "text": "stale", "citations": []})

    out = _post(session.bot_id, "Bob", "wait hold on a second")

    assert session.assistant_interrupted is True
    msgs = store.drain_avatar_messages(session)
    stops = [m for m in msgs if m.get("type") == "stop"]
    assert stops and stops[0]["generation_id"] == gen
    # The stale speak chunk was dropped, and this generic line was not answered.
    assert all(m.get("type") != "speak" for m in msgs)
    assert out["spoke"] is False


def test_no_barge_in_when_not_speaking(session, monkeypatch):
    _stub_answer(monkeypatch, ["ok"])
    _post(session.bot_id, "Bob", "just chatting here")
    msgs = store.drain_avatar_messages(session)
    assert all(m.get("type") != "stop" for m in msgs)


# ── staleness: a chunk from a superseded turn is dropped ──
def test_is_stale_after_interrupt_and_new_turn():
    s = store.Session(bot_id="b", meeting_url="m")
    object.__setattr__(s, "_persist_enabled", False)
    gen = s.begin_speaking()
    assert s.is_stale(gen) is False
    s.interrupt()
    assert s.is_stale(gen) is True          # interrupted
    new_gen = s.begin_speaking()
    assert s.is_stale(gen) is True          # superseded by a newer turn
    assert s.is_stale(new_gen) is False


def test_streaming_loop_drops_chunks_after_interrupt():
    # Mirrors the webhook loop guard: once interrupted mid-stream, stop speaking.
    s = store.Session(bot_id="b2", meeting_url="m")
    object.__setattr__(s, "_persist_enabled", False)
    gen = s.begin_speaking()
    spoken = []
    for sentence in ["one.", "two.", "three."]:
        if s.is_stale(gen):
            break
        spoken.append(sentence)
        if sentence == "one.":
            s.interrupt()  # a new user line arrives during the first sentence
    assert spoken == ["one."]


# ── repetition guard ──
def test_volunteered_repeat_stays_silent(session, monkeypatch):
    _stub_answer(monkeypatch, ["Elevated access needs a security lead sign-off."])
    first = _post(session.bot_id, "Bob", "Laura, what is the approval process?")
    assert first["spoke"] is True

    session.last_spoke_at = 0.0  # simulate cooldown elapsed
    # A different person re-raises the same ask, not addressing her -> don't repeat.
    out = _post(session.bot_id, "Alice", "what is the approval process?")
    assert out["spoke"] is False
    assert out["reason"] == "repetition"


def test_same_speaker_repeat_is_shortened(session, monkeypatch):
    _stub_answer(monkeypatch, ["Elevated access needs a security lead sign-off."])
    _post(session.bot_id, "Bob", "Laura, what is the approval process?")
    store.drain_avatar_messages(session)  # clear the first answer

    session.last_spoke_at = 0.0  # simulate cooldown elapsed
    _stub_answer(monkeypatch, ["Elevated access needs a security lead sign-off."])
    _post(session.bot_id, "Bob", "Laura, what is the approval process?")
    msgs = store.drain_avatar_messages(session)
    assert msgs[0]["text"] == "Same as before —"
