"""TTS voice fallback: configured voice -> stock ElevenLabs -> edge-tts."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import tts  # noqa: E402
from app.config import settings  # noqa: E402


class _Resp:
    status_code = 200

    def json(self):
        return {"audio_base64": "QUJD", "alignment": {}}


def _synth_factory(fail_voices: dict, calls: list):
    async def fake_synth(voice_id: str, text: str):
        calls.append(voice_id)
        if voice_id in fail_voices:
            resp = httpx.Response(fail_voices[voice_id], request=httpx.Request("POST", "http://x"))
            raise httpx.HTTPStatusError("blocked", request=resp.request, response=resp)
        return _Resp()

    return fake_synth


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k")
    tts._el_broken_voices.clear()
    yield
    tts._el_broken_voices.clear()


def test_blocked_custom_voice_degrades_to_stock_not_edge(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "custom-blocked")
    monkeypatch.setattr(tts, "_el_synthesize", _synth_factory({"custom-blocked": 402}, calls))
    r = asyncio.run(tts._tts_elevenlabs("hello"))
    assert r is not None and r["engine"] == "elevenlabs"  # NOT edge-tts
    assert calls == ["custom-blocked", tts._el_fallback_voice()]
    assert "custom-blocked" in tts._el_broken_voices


def test_broken_voice_cached_no_doomed_roundtrip(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "custom-blocked")
    monkeypatch.setattr(tts, "_el_synthesize", _synth_factory({"custom-blocked": 402}, calls))
    asyncio.run(tts._tts_elevenlabs("one"))
    asyncio.run(tts._tts_elevenlabs("two"))
    assert calls == ["custom-blocked", tts._el_fallback_voice(), tts._el_fallback_voice()]


def test_stock_voice_failure_falls_back_to_edge(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "")
    monkeypatch.setattr(tts, "_el_synthesize", _synth_factory({tts._el_fallback_voice(): 500}, calls))
    assert asyncio.run(tts._tts_elevenlabs("hello")) is None  # edge-tts fallback


def test_fallback_voice_is_configurable(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "custom-blocked")
    monkeypatch.setattr(settings, "elevenlabs_fallback_voice_id", "sarah-voice")
    monkeypatch.setattr(tts, "_el_synthesize", _synth_factory({"custom-blocked": 402}, calls))
    r = asyncio.run(tts._tts_elevenlabs("hello"))
    assert r is not None and calls == ["custom-blocked", "sarah-voice"]


def test_transient_error_does_not_mark_voice_broken(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "custom-flaky")
    monkeypatch.setattr(tts, "_el_synthesize", _synth_factory({"custom-flaky": 500}, calls))
    r = asyncio.run(tts._tts_elevenlabs("hello"))
    assert r is not None and r["engine"] == "elevenlabs"  # retried on stock
    assert "custom-flaky" not in tts._el_broken_voices  # 500 is transient
