"""Standalone spike: talk to Gemini Live (gemini-live-2.5-flash) on Vertex AI.

NOT part of the Laura app — nothing imports this. It exists so you can *hear* the
realtime voice and decide whether it's worth wiring into the live meeting path.
The live meeting contract (ws/<conversation_id>, {type:"speak"}, recall_client)
is untouched.

Run:
    pip install websockets            # spike-only dep
    gcloud auth login                 # once; the proxy mints tokens from this
    python backend/spikes/vertex_live/server.py
    # open http://localhost:8777

Architecture: browser mic (PCM 16 kHz) --ws--> this proxy --ws--> Gemini Live
(global host). Gemini streams PCM 24 kHz back; the proxy relays it to the browser
for playback. Automatic VAD handles turn-taking. Auth: a short-lived bearer minted
from your gcloud login (dev only). For prod you'd use a service account + the
app's llm._vertex_token().
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import socketserver
import subprocess
import threading
import time

import websockets

HTTP_PORT = int(os.environ.get("SPIKE_HTTP_PORT", "8777"))
WS_PORT = int(os.environ.get("SPIKE_WS_PORT", "8778"))
PROJECT = os.environ.get("VERTEX_PROJECT", "868562221752")
LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
MODEL = os.environ.get("VERTEX_LIVE_MODEL", "gemini-live-2.5-flash")
LANG = os.environ.get("SPIKE_LANG", "it-IT")

# Bidi runs ONLY on the global host — the region-prefixed host 1008s.
GEMINI_URL = (
    "wss://aiplatform.googleapis.com/ws/"
    "google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent"
)
MODEL_PATH = f"projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}"
HERE = os.path.dirname(os.path.abspath(__file__))

SYSTEM = (
    "Sei Laura, un'assistente che partecipa a una riunione. Parla in italiano, "
    "tono naturale, caldo e conciso. Frasi brevi, come al telefono."
)

_tok = {"v": "", "ts": 0.0}


def _token() -> str:
    """Mint a bearer from the current gcloud login (cached ~40 min)."""
    now = time.time()
    if _tok["v"] and now - _tok["ts"] < 2400:
        return _tok["v"]
    out = subprocess.check_output(
        ["gcloud", "auth", "print-access-token"], text=True
    ).strip()
    _tok.update(v=out, ts=now)
    return out


SETUP = {
    "setup": {
        "model": MODEL_PATH,
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"languageCode": LANG},
        },
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "outputAudioTranscription": {},
        "inputAudioTranscription": {},
    }
}


async def _pump_browser_to_gemini(browser, gemini):
    async for raw in browser:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        kind = msg.get("type")
        if kind == "audio":  # base64 PCM16 @16kHz from the mic
            await gemini.send(
                json.dumps(
                    {
                        "realtimeInput": {
                            "mediaChunks": [
                                {"mimeType": "audio/pcm;rate=16000", "data": msg["data"]}
                            ]
                        }
                    }
                )
            )
        elif kind == "text":  # typed message (test without a mic)
            await gemini.send(
                json.dumps(
                    {
                        "clientContent": {
                            "turns": [{"role": "user", "parts": [{"text": msg["text"]}]}],
                            "turnComplete": True,
                        }
                    }
                )
            )


async def _pump_gemini_to_browser(gemini, browser):
    async for raw in gemini:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        m = json.loads(raw)
        sc = m.get("serverContent")
        if not sc:
            continue
        if sc.get("interrupted"):
            await browser.send(json.dumps({"type": "interrupted"}))
        for part in (sc.get("modelTurn") or {}).get("parts", []):
            data = (part.get("inlineData") or {}).get("data")
            if data:
                await browser.send(json.dumps({"type": "audio", "data": data}))
        ot = (sc.get("outputTranscription") or {}).get("text")
        if ot:
            await browser.send(json.dumps({"type": "subtitle", "who": "laura", "text": ot}))
        it = (sc.get("inputTranscription") or {}).get("text")
        if it:
            await browser.send(json.dumps({"type": "subtitle", "who": "you", "text": it}))
        if sc.get("turnComplete"):
            await browser.send(json.dumps({"type": "turn_complete"}))


async def handle(browser):
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
                await browser.send(
                    json.dumps({"type": "error", "message": f"setup: {first}"})
                )
                return
            await browser.send(json.dumps({"type": "ready"}))
            await asyncio.gather(
                _pump_browser_to_gemini(browser, gemini),
                _pump_gemini_to_browser(gemini, browser),
            )
    except Exception as e:  # noqa: BLE001
        try:
            await browser.send(json.dumps({"type": "error", "message": str(e)[:200]}))
        except Exception:
            pass


def _serve_http():
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=HERE
    )
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", HTTP_PORT), handler) as httpd:
        httpd.serve_forever()


async def main():
    threading.Thread(target=_serve_http, daemon=True).start()
    print(f"[spike] model={MODEL}  location={LOCATION}")
    print(f"[spike] open  http://localhost:{HTTP_PORT}")
    async with websockets.serve(handle, "127.0.0.1", WS_PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[spike] bye")
