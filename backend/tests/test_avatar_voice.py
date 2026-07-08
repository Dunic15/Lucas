"""Per-avatar voice: each avatar speaks in its OWN ElevenLabs voice, not the
global default. Cedric = Eric; Laura = the .env voice. Key-free (ElevenLabs
stubbed at the synth seam). Async helpers are driven with asyncio.run, matching
the rest of the suite (no pytest-asyncio dependency)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, tts
from app.config import settings


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    tts._tts_cache.clear()
    monkeypatch.setattr(settings, "elevenlabs_api_key", "test-key")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "GLOBAL_DEFAULT")
    yield
    tts._tts_cache.clear()


def _stub_synth(monkeypatch):
    """Record which voice_id each synth call used; return a minimal payload."""
    used: list[str] = []

    async def fake(voice_id, text):
        used.append(voice_id)

        class R:
            def json(self):
                return {"audio_base64": "AAAA", "alignment": {}}

        return R()

    monkeypatch.setattr(tts, "_el_synthesize", fake)
    return used


def test_cedric_yaml_has_eric_voice():
    assert avatars.load("cedric").elevenlabs_voice_id == "cjVigY5qzO86Huf0OWal"


def test_synthesize_uses_avatar_voice(monkeypatch):
    used = _stub_synth(monkeypatch)
    asyncio.run(tts.synthesize_cached("Hello there", el_voice="cjVigY5qzO86Huf0OWal"))
    assert used == ["cjVigY5qzO86Huf0OWal"]  # Eric, not GLOBAL_DEFAULT


def test_voice_is_part_of_cache_key(monkeypatch):
    used = _stub_synth(monkeypatch)
    # Same text, two different avatar voices → two synths, not a cross-voice hit.
    asyncio.run(tts.synthesize_cached("Same line", el_voice="cjVigY5qzO86Huf0OWal"))
    asyncio.run(tts.synthesize_cached("Same line", el_voice="OTHER_VOICE"))
    assert used == ["cjVigY5qzO86Huf0OWal", "OTHER_VOICE"]
    # A third call in Eric's voice is a cache hit (no new synth).
    asyncio.run(tts.synthesize_cached("Same line", el_voice="cjVigY5qzO86Huf0OWal"))
    assert len(used) == 2


def test_global_default_normalizes_to_shared_key(monkeypatch):
    used = _stub_synth(monkeypatch)
    # Passing the global voice explicitly must share the "" prewarm key, so an
    # avatar with no override reuses Laura's prewarmed lines.
    asyncio.run(tts.synthesize_cached("Shared ack", el_voice="GLOBAL_DEFAULT"))
    asyncio.run(tts.synthesize_cached("Shared ack", el_voice=""))
    assert len(used) == 1  # second call hit the cache under the same "" key


def test_cached_payload_respects_voice():
    tts._cache_put("cjVigY5qzO86Huf0OWal|Noted.", {"audio": "X", "engine": "elevenlabs"})
    assert tts.cached_payload("Noted.", el_voice="cjVigY5qzO86Huf0OWal") is not None
    # Laura's key is absent → miss (no cross-voice bleed).
    assert tts.cached_payload("Noted.", el_voice="") is None
