"""Design B spike: Gemini Live = ears + turn-taking, Laura's gate decides, ElevenLabs speaks.

The combination you'd actually ship: keep Laura's control layer (decision.py) and
her ElevenLabs brand voice, and let Gemini Live upgrade the weak part — natural
ears + turn detection. NOT wired into the live meeting path; this imports app.* to
reuse the *real* gate + voice config, and runs off the repo .env.

    browser mic (PCM16 16 kHz) --ws--> proxy --ws--> Gemini Live  [responseModalities=TEXT]
      on end-of-turn (Gemini VAD):
        user transcript  -> decision.detect_wake(LAURA)     # the real gate
        if addressed     -> reply text (Gemini) -> ElevenLabs (Laura's voice) -> browser plays
        else             -> stay silent                     # the gate held her back

Run:
    cd backend && \
    /path/to/.venv/bin/python spikes/vertex_live_designb/server.py
    # open http://localhost:8779
"""
from __future__ import annotations

import asyncio
import base64
import functools
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time

import httpx
import websockets

# Reuse Laura's real modules (gate + voice config). Repo layout: this file is at
# backend/spikes/vertex_live_designb/, so backend/ is three parents up.
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _BACKEND)
from app import avatars, decision  # noqa: E402
from app.config import settings  # noqa: E402

HTTP_PORT = int(os.environ.get("SPIKE_HTTP_PORT", "8779"))
WS_PORT = int(os.environ.get("SPIKE_WS_PORT", "8780"))
PROJECT = os.environ.get("VERTEX_PROJECT", "868562221752")
LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
MODEL = os.environ.get("VERTEX_LIVE_MODEL", "gemini-live-2.5-flash")
AVATAR_ID = os.environ.get("SPIKE_AVATAR", "laura")

GEMINI_URL = (
    "wss://aiplatform.googleapis.com/ws/"
    "google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent"
)
MODEL_PATH = f"projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}"
HERE = os.path.dirname(os.path.abspath(__file__))
LAURA = avatars.load(AVATAR_ID)

PERSONA = (
    f"Sei {LAURA.name if hasattr(LAURA, 'name') else 'Laura'}, un'assistente che "
    "partecipa a una riunione. Rispondi in italiano, tono naturale e conciso, "
    "frasi brevi come al telefono. Se non sai qualcosa, dillo."
)

# Gemini as ears + brain, but TEXT out — the voice is ElevenLabs, downstream.
SETUP = {
    "setup": {
        "model": MODEL_PATH,
        "generationConfig": {"responseModalities": ["TEXT"]},
        "systemInstruction": {"parts": [{"text": PERSONA}]},
        "inputAudioTranscription": {},
    }
}

_tok = {"v": "", "ts": 0.0}


def _token() -> str:
    now = time.time()
    if _tok["v"] and now - _tok["ts"] < 2400:
        return _tok["v"]
    out = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    _tok.update(v=out, ts=now)
    return out


async def _elevenlabs(text: str) -> bytes | None:
    """Synthesize `text` in Laura's ElevenLabs voice -> mp3 bytes (None if no key)."""
    if not settings.elevenlabs_api_key:
        return None
    voice = settings.elevenlabs_voice_id or "FGY2WhTYpPnrIDTdsKH5"
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            headers={"xi-api-key": settings.elevenlabs_api_key},
            json={"text": text, "model_id": settings.elevenlabs_tts_model or "eleven_flash_v2_5"},
        )
        r.raise_for_status()
        return r.content


async def _finish_turn(browser, st):
    """End-of-turn: run the real gate, then speak with ElevenLabs (or stay silent)."""
    user = (st["user"] or "").strip()
    reply = (st["reply"] or "").strip()
    st["user"], st["reply"] = "", ""
    if not user and not reply:
        return
    addressed, _stripped = decision.detect_wake(LAURA, user) if user else (True, "")
    await browser.send(json.dumps({"type": "user", "text": user}))
    await browser.send(
        json.dumps({"type": "gate", "addressed": bool(addressed), "require_wake": st["require_wake"]})
    )
    # The gate: when wake is required and she wasn't addressed, decision.py holds
    # her back — exactly as in a real meeting. Otherwise she answers.
    if st["require_wake"] and not addressed:
        await browser.send(json.dumps({"type": "silent"}))
        return
    if not reply:
        return
    await browser.send(json.dumps({"type": "reply", "text": reply}))
    try:
        audio = await _elevenlabs(reply)
    except Exception as e:  # noqa: BLE001
        await browser.send(json.dumps({"type": "error", "message": f"elevenlabs: {str(e)[:120]}"}))
        return
    if audio:
        await browser.send(
            json.dumps({"type": "tts_audio", "data": base64.b64encode(audio).decode()})
        )
    else:
        await browser.send(json.dumps({"type": "error", "message": "no ElevenLabs key"}))


async def _browser_to_gemini(browser, gemini, st):
    async for raw in browser:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        kind = msg.get("type")
        if kind == "audio":
            await gemini.send(
                json.dumps(
                    {"realtimeInput": {"mediaChunks": [
                        {"mimeType": "audio/pcm;rate=16000", "data": msg["data"]}
                    ]}}
                )
            )
        elif kind == "text":  # typed — the gate reads this directly
            st["user"] = msg["text"]
            await gemini.send(
                json.dumps({"clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": msg["text"]}]}],
                    "turnComplete": True,
                }})
            )
        elif kind == "config":
            st["require_wake"] = bool(msg.get("require_wake"))


async def _gemini_to_browser(gemini, browser, st):
    async for raw in gemini:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        m = json.loads(raw)
        sc = m.get("serverContent")
        if not sc:
            continue
        it = (sc.get("inputTranscription") or {}).get("text")
        if it:  # audio path: accumulate what the human said (for the gate)
            st["user"] = (st["user"] or "") + it
        for part in (sc.get("modelTurn") or {}).get("parts", []):
            if "text" in part:
                st["reply"] = (st["reply"] or "") + part["text"]
        if sc.get("turnComplete"):
            await _finish_turn(browser, st)


async def handle(browser):
    st = {"user": "", "reply": "", "require_wake": False}
    try:
        token = _token()
    except Exception as e:  # noqa: BLE001
        await browser.send(json.dumps({"type": "error", "message": f"token: {e}"}))
        return
    try:
        async with websockets.connect(
            GEMINI_URL,
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=None,
            open_timeout=20,
        ) as gemini:
            await gemini.send(json.dumps(SETUP))
            first = json.loads(await gemini.recv())
            if "setupComplete" not in first:
                await browser.send(json.dumps({"type": "error", "message": f"setup: {first}"}))
                return
            await browser.send(
                json.dumps({"type": "ready", "voice": bool(settings.elevenlabs_api_key), "avatar": AVATAR_ID})
            )
            await asyncio.gather(
                _browser_to_gemini(browser, gemini, st),
                _gemini_to_browser(gemini, browser, st),
            )
    except Exception as e:  # noqa: BLE001
        try:
            await browser.send(json.dumps({"type": "error", "message": str(e)[:200]}))
        except Exception:
            pass


def _serve_http():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=HERE)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", HTTP_PORT), handler) as httpd:
        httpd.serve_forever()


async def main():
    threading.Thread(target=_serve_http, daemon=True).start()
    print(f"[spike-B] avatar={AVATAR_ID}  gate=decision.detect_wake  voice=ElevenLabs({bool(settings.elevenlabs_api_key)})")
    print(f"[spike-B] Gemini ears={MODEL} (TEXT out)  location={LOCATION}")
    print(f"[spike-B] open  http://localhost:{HTTP_PORT}")
    async with websockets.serve(handle, "127.0.0.1", WS_PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[spike-B] bye")
