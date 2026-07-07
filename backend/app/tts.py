"""Text-to-speech for the open-source avatar page (/talk) — replaces Anam's voice.

Preferred: ElevenLabs with-timestamps (set ELEVENLABS_API_KEY) — premium voice AND
character-level timing alignment, which the page turns into accurate per-word
lip-sync. Voice fallback chain: configured voice -> stock ElevenLabs voice ->
edge-tts (free, keyless; the page spreads word timings evenly — approximate
lip-sync). Response is JSON either way:
    {"audio": <base64 mp3>, "words": [...]|null, "wtimes": [ms]|null,
     "wdurations": [ms]|null, "engine": "elevenlabs"|"edge", "tts_ms": <int>}

Split out of main.py: a self-contained concern with no coupling to the live
meeting path, so it lives behind its own APIRouter.
"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import settings

router = APIRouter()

_TTS_DEFAULT_VOICE = "en-US-AriaNeural"


class TtsRequest(BaseModel):
    text: str
    avatar_id: str = "laura"
    voice: str = ""


def _words_from_alignment(alignment: dict) -> tuple[list, list, list]:
    """Character alignment -> per-word (words, start_ms, duration_ms) for lip-sync."""
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    words, wtimes, wdurs = [], [], []
    cur, w_start, w_end = "", 0.0, 0.0
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append(cur)
                wtimes.append(int(w_start * 1000))
                wdurs.append(max(1, int((w_end - w_start) * 1000)))
                cur = ""
        else:
            if not cur:
                w_start = s
            cur += ch
            w_end = e
    if cur:
        words.append(cur)
        wtimes.append(int(w_start * 1000))
        wdurs.append(max(1, int((w_end - w_start) * 1000)))
    return words, wtimes, wdurs


# Persistent connection to ElevenLabs: a fresh TLS handshake per sentence costs
# ~150ms on every utterance. One keep-alive client removes it permanently.
_el_client: httpx.AsyncClient | None = None


def _get_el_client() -> httpx.AsyncClient:
    global _el_client
    if _el_client is None:
        _el_client = httpx.AsyncClient(timeout=20.0)
    return _el_client


def _el_fallback_voice() -> str:
    """Voice to use when the configured one is blocked (settings-driven so the
    owner can pick e.g. Sarah while a premium voice waits on a plan upgrade)."""
    return settings.elevenlabs_fallback_voice_id or "FGY2WhTYpPnrIDTdsKH5"


# Configured voices that failed hard (403/402/404: plan tier, licensing, or a
# deleted voice). Remembered per-process so we don't pay a doomed round-trip on
# every sentence; cleared on restart/deploy so an upgraded plan is retried.
_el_broken_voices: set[str] = set()


async def _el_synthesize(voice_id: str, text: str) -> httpx.Response:
    r = await _get_el_client().post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        "/with-timestamps?output_format=mp3_44100_128",
        headers={"xi-api-key": settings.elevenlabs_api_key},
        json={"text": text, "model_id": settings.elevenlabs_tts_model},
    )
    r.raise_for_status()
    return r


async def _tts_elevenlabs(text: str) -> dict | None:
    """ElevenLabs with-timestamps. Returns the JSON payload, or None to fall back.

    Voice fallback chain: configured voice -> stock ElevenLabs voice -> None
    (edge-tts). A custom voice blocked by the account's plan (402) or licensing
    must degrade to the STOCK ElevenLabs voice — same engine, real word timings
    — not all the way down to robotic edge-tts.
    """
    if not settings.elevenlabs_api_key:
        return None
    voice_id = settings.elevenlabs_voice_id or _el_fallback_voice()
    if voice_id in _el_broken_voices:
        voice_id = _el_fallback_voice()
    try:
        try:
            r = await _el_synthesize(voice_id, text)
        except httpx.HTTPStatusError as e:
            if voice_id == _el_fallback_voice():
                raise
            status = e.response.status_code
            if status in (402, 403, 404):  # plan tier / licensing / deleted voice
                _el_broken_voices.add(voice_id)
            print(f"[tts] voice {voice_id} unavailable (HTTP {status}) — using stock voice", flush=True)
            r = await _el_synthesize(_el_fallback_voice(), text)
        data = r.json()
        alignment = data.get("normalized_alignment") or data.get("alignment") or {}
        words, wtimes, wdurs = _words_from_alignment(alignment)
        return {
            "audio": data["audio_base64"],
            "words": words or None,
            "wtimes": wtimes or None,
            "wdurations": wdurs or None,
            "engine": "elevenlabs",
        }
    except Exception as e:  # noqa: BLE001 — any EL failure degrades to edge-tts
        print(f"[tts] elevenlabs failed, falling back to edge-tts: {e}", flush=True)
        return None


@router.post("/tts")
async def tts(req: TtsRequest) -> Response:
    text = (req.text or "").strip()[:2000]
    if not text:
        return Response(status_code=204)

    import base64

    import edge_tts

    # tts_ms: synthesis latency for the metrics picture (issue #3). A duration
    # only — the text itself is never logged or exported.
    t0 = time.perf_counter()
    el = await _tts_elevenlabs(text)
    if el is not None:
        el["tts_ms"] = int((time.perf_counter() - t0) * 1000)
        return JSONResponse(el, headers={"Cache-Control": "no-store"})

    voice = (req.voice or "").strip() or _TTS_DEFAULT_VOICE
    audio = bytearray()
    try:
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
    except Exception as e:
        return JSONResponse({"error": f"tts failed: {e}"}, status_code=502)
    if not audio:
        return JSONResponse({"error": "tts produced no audio"}, status_code=502)
    return JSONResponse(
        {
            "audio": base64.b64encode(bytes(audio)).decode(),
            "words": None,
            "wtimes": None,
            "wdurations": None,
            "engine": "edge",
            "tts_ms": int((time.perf_counter() - t0) * 1000),
        },
        headers={"Cache-Control": "no-store"},
    )
