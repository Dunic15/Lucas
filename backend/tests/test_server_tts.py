"""Server-side TTS rides the speak message: audio attached, strict sentence
order, graceful degradation to the page-side /tts path. No vendors/keys."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store, tts  # noqa: E402
from app.config import settings  # noqa: E402


def _session(bot_id: str = "tts-bot") -> store.Session:
    s = store.Session(bot_id=bot_id, meeting_url="https://meet.example/x")
    object.__setattr__(s, "_persist_enabled", False)
    return s


_PAYLOAD = {
    "audio": "QUJD",  # base64("ABC"); never decoded server-side
    "words": ["ciao", "a", "tutti"],
    "wtimes": [0, 400, 800],
    "wdurations": [300, 300, 1200],
    "engine": "elevenlabs",
}


def test_speak_message_carries_server_audio():
    s = _session()
    ok = asyncio.run(main._make_avatar_speak(s, "Ciao a tutti.", audio=dict(_PAYLOAD)))
    assert ok is True
    msg = s.pending_messages[-1]
    assert msg["type"] == "speak"
    assert msg["audio"] == "QUJD"
    assert msg["words"] == ["ciao", "a", "tutti"]
    assert msg["engine"] == "elevenlabs"
    # Barge-in window comes from the REAL duration (0.8s + 1.2s + 0.3s grace
    # = 2.3s), not the words/second estimate (3 words would be ~1.2s).
    assert s.speaking_until - time.time() > 1.9


def test_speak_without_audio_has_no_audio_fields():
    s = _session()
    asyncio.run(main._make_avatar_speak(s, "Solo testo, come prima."))
    assert "audio" not in s.pending_messages[-1]


def test_pipelined_speaks_preserve_sentence_order(monkeypatch):
    """Sentence 1's synth is SLOW, sentence 2's is instant; sends must still
    happen in sentence order (the prev-chain is the ordering guarantee)."""
    s = _session()

    async def fake_synth(text):
        await asyncio.sleep(0.15 if "first" in text else 0.0)
        return dict(_PAYLOAD)

    monkeypatch.setattr(tts, "synthesize_cached", fake_synth)

    async def run():
        gen = store.bump_speech_generation(s)
        t1 = asyncio.create_task(
            main._speak_with_audio(s, "the first sentence", force=True, generation=gen, prev=None)
        )
        t2 = asyncio.create_task(
            main._speak_with_audio(s, "the second sentence", force=True, generation=gen, prev=t1)
        )
        return await asyncio.gather(t1, t2)

    assert asyncio.run(run()) == [True, True]
    texts = [m["text"] for m in s.pending_messages if m["type"] == "speak"]
    assert texts == ["the first sentence", "the second sentence"]


def test_synth_failure_degrades_to_plain_speak(monkeypatch):
    s = _session()

    async def broken(text):
        raise RuntimeError("ElevenLabs down")

    monkeypatch.setattr(tts, "synthesize_cached", broken)

    async def run():
        gen = store.bump_speech_generation(s)
        return await main._speak_with_audio(s, "still spoken", force=True, generation=gen, prev=None)

    assert asyncio.run(run()) is True  # she still speaks; page-side /tts covers it
    assert "audio" not in s.pending_messages[-1]


def test_stale_generation_skips_synth_and_send(monkeypatch):
    """A cancelled turn's sentence must be dropped AND not pay for synthesis."""
    s = _session()
    calls = {"n": 0}

    async def counting(text):
        calls["n"] += 1
        return dict(_PAYLOAD)

    monkeypatch.setattr(tts, "synthesize_cached", counting)

    async def run():
        gen = store.bump_speech_generation(s)
        store.bump_speech_generation(s)  # superseded before the task runs
        return await main._speak_with_audio(s, "late sentence", force=True, generation=gen, prev=None)

    assert asyncio.run(run()) is False
    assert calls["n"] == 0
    assert s.pending_messages == []


def test_synthesize_cached_and_cached_payload(monkeypatch):
    calls = {"n": 0}

    async def fake_el(text, el_voice=""):
        calls["n"] += 1
        return {k: v for k, v in _PAYLOAD.items() if k != "tts_ms"}

    monkeypatch.setattr(tts, "_tts_elevenlabs", fake_el)
    monkeypatch.setattr(settings, "elevenlabs_api_key", "test-key")
    tts._tts_cache.clear()
    try:
        assert tts.cached_payload("Mm-hm.") is None  # cold cache: no waiting allowed
        p1 = asyncio.run(tts.synthesize_cached("Mm-hm."))
        assert p1 and p1["audio"] == "QUJD" and calls["n"] == 1
        p2 = asyncio.run(tts.synthesize_cached("Mm-hm."))
        assert p2["tts_ms"] == 0 and calls["n"] == 1  # cache hit, no second call
        assert tts.cached_payload("Mm-hm.")["audio"] == "QUJD"
    finally:
        tts._tts_cache.clear()


def test_synthesize_cached_none_without_key(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    tts._tts_cache.clear()
    assert asyncio.run(tts.synthesize_cached("hello there friends")) is None
