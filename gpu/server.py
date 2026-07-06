"""Laura photoreal avatar — GPU streaming server (Stage 2).

Runs on the laura-gpu EC2 box (launch template `laura-gpu`, g5.xlarge) and turns
TTS audio into a photoreal talking-head video stream that photoreal.html renders
as the bot camera. The BRAIN and TTS stay on the App Runner backend — this box
only does face rendering, so it can be stopped whenever no meeting is running
(GPU idle = money; same rule as the Recall meter).

Two engines behind one seam (pick with AVATAR_ENGINE=stub|musetalk):

  stub      — no GPU needed. Streams the reference portrait with a subtle
              breathing sway. Exists so the ENTIRE pipeline (page, websocket,
              framing, audio sync, meeting mode) is testable on a laptop today.
  musetalk  — the real thing: MuseTalk (open source, Tencent) lip-syncs the
              reference face to the audio in near-real-time on the GPU.
              Install via setup.sh; expect launch-day tuning.

WebSocket protocol (single socket, /stream):
  client -> server:  {"type":"speak","audio_b64":"<mp3 base64>"}
  server -> client:  text {"type":"hello","mode":...,"fps":N}
                     text {"type":"talk_start"}   (page starts audio playback)
                     text {"type":"talk_end"}
                     binary <JPEG frame>          (continuous, idle or talking)

Run:  AVATAR_ENGINE=stub REFERENCE_IMAGE=assets/reference.jpg \
      python3 server.py            (listens on 0.0.0.0:8080)
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from PIL import Image

ENGINE = os.environ.get("AVATAR_ENGINE", "stub").lower()
REFERENCE_IMAGE = os.environ.get("REFERENCE_IMAGE", "assets/reference.jpg")
FPS = int(os.environ.get("STREAM_FPS", "12" if ENGINE == "stub" else "25"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "82"))

app = FastAPI()


class StubEngine:
    """No-GPU engine: reference portrait + subtle breathing sway.

    Proves transport, framing, and audio timing without any ML. The page can't
    tell the difference protocol-wise — swap AVATAR_ENGINE and nothing else.
    """

    def __init__(self, image_path: str) -> None:
        img = Image.open(image_path).convert("RGB")
        # Pre-render a small loop of frames (sway + breath) once; cheap to serve.
        self.frames: list[bytes] = []
        w, h = img.size
        n = 48
        for i in range(n):
            t = i / n * 2 * math.pi
            zoom = 1.0 + 0.006 * math.sin(t)          # breathing
            dx = 2.0 * math.sin(t * 0.5)               # slow sway
            zw, zh = int(w / zoom), int(h / zoom)
            left = max(0, int((w - zw) / 2 + dx))
            top = max(0, int((h - zh) / 2))
            frame = img.crop((left, top, left + zw, top + zh)).resize((w, h))
            buf = io.BytesIO()
            frame.save(buf, "JPEG", quality=JPEG_QUALITY)
            self.frames.append(buf.getvalue())
        self.i = 0

    async def start(self) -> None:  # engine warmup seam
        return

    def next_idle_frame(self) -> bytes:
        self.i = (self.i + 1) % len(self.frames)
        return self.frames[self.i]

    async def talk_frames(self, audio_mp3: bytes):
        """Stub 'talking': same idle loop for the clip's rough duration.

        Yields frames at FPS; duration is estimated from mp3 size (~4KB/s at
        32kbps mono — rough is fine, the page ends on talk_end anyway).
        """
        seconds = max(0.8, len(audio_mp3) / 4000.0)
        for _ in range(int(seconds * FPS)):
            yield self.next_idle_frame()


class MuseTalkEngine:
    """Real engine: MuseTalk real-time lip-sync (see setup.sh for install).

    Interface-compatible with StubEngine. The heavy imports happen in start()
    so the module loads on any machine. NOTE: written against MuseTalk's
    realtime inference API; expect to tune chunk sizes / fps on the actual GPU
    box on launch day — that's normal for these models.
    """

    def __init__(self, image_path: str) -> None:
        self.image_path = image_path
        self.pipeline = None
        self._stub = StubEngine(image_path)  # idle frames while not talking

    async def start(self) -> None:
        # Deferred import: only exists on the GPU box after setup.sh.
        from musetalk_adapter import MuseTalkPipeline  # noqa: PLC0415

        self.pipeline = MuseTalkPipeline(self.image_path)
        await asyncio.to_thread(self.pipeline.warmup)

    def next_idle_frame(self) -> bytes:
        return self._stub.next_idle_frame()

    async def talk_frames(self, audio_mp3: bytes):
        """Yield MuseTalk-generated JPEG frames for this audio clip."""
        async for frame in self.pipeline.stream(audio_mp3, fps=FPS,
                                                jpeg_quality=JPEG_QUALITY):
            yield frame


engine = (MuseTalkEngine if ENGINE == "musetalk" else StubEngine)(REFERENCE_IMAGE)


@app.on_event("startup")
async def _warmup() -> None:
    await engine.start()
    print(f"[gpu-avatar] engine={ENGINE} fps={FPS} ref={REFERENCE_IMAGE}", flush=True)


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "engine": ENGINE, "fps": FPS})


@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    await ws.accept()
    await ws.send_text(json.dumps({"type": "hello", "mode": ENGINE, "fps": FPS}))
    speak_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def reader() -> None:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "speak" and msg.get("audio_b64"):
                await speak_queue.put(base64.b64decode(msg["audio_b64"]))

    reader_task = asyncio.create_task(reader())
    frame_interval = 1.0 / FPS
    try:
        while True:
            try:
                audio = speak_queue.get_nowait()
            except asyncio.QueueEmpty:
                audio = None

            if audio is not None:
                await ws.send_text(json.dumps({"type": "talk_start"}))
                async for frame in engine.talk_frames(audio):
                    t0 = time.perf_counter()
                    await ws.send_bytes(frame)
                    # keep real-time pacing even if generation is faster
                    delay = frame_interval - (time.perf_counter() - t0)
                    if delay > 0:
                        await asyncio.sleep(delay)
                await ws.send_text(json.dumps({"type": "talk_end"}))
            else:
                await ws.send_bytes(engine.next_idle_frame())
                await asyncio.sleep(frame_interval)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader_task.cancel()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
