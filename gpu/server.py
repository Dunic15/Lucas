"""Laura photoreal avatar — GPU streaming server (Stage 2).

Runs on the laura-gpu EC2 box (launch template `laura-gpu`, g5.xlarge) and turns
TTS audio into a photoreal talking-head video stream that photoreal.html renders
as the bot camera. The BRAIN and TTS stay on the App Runner backend — this box
only does face rendering, so it can be stopped whenever no meeting is running
(GPU idle = money; same rule as the Recall meter).

Three engines behind one seam (pick with AVATAR_ENGINE=stub|musetalk|ditto):

  stub      — no GPU needed. Streams the reference portrait with a subtle
              breathing sway. Exists so the ENTIRE pipeline (page, websocket,
              framing, audio sync, meeting mode) is testable on a laptop today.
  musetalk  — MuseTalk (open source, Tencent): lip-sync only, head stays
              still. Install via setup.sh.
  ditto     — Ditto (open source, Ant Group; Apache-2.0): lip-sync PLUS head
              motion and expressions — the chosen production face
              (owner-approved 2026-07-09). TRT online pipeline, Ampere+ GPU.
              See Dockerfile.ditto + DITTO-LIVE.md; expect launch-day tuning.

WebSocket protocol (single socket, /stream):
  client -> server:  {"type":"speak","audio_b64":"<mp3 base64>","generation_id":N}
                     {"type":"stop","generation_id":N}
  server -> client:  text {"type":"hello","mode":...,"fps":N}
                     text {"type":"talk_start","generation_id":N}
                     text {"type":"talk_end","generation_id":N,"cancelled":bool}
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
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from PIL import Image

ENGINE = os.environ.get("AVATAR_ENGINE", "stub").lower()
REFERENCE_IMAGE = os.environ.get("REFERENCE_IMAGE", "assets/reference.jpg")
FPS = int(os.environ.get("STREAM_FPS", "12" if ENGINE == "stub" else "25"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "82"))
# Frames SENT per second (0 = every generated frame). Recall delivers at most
# 15fps in-meeting: pushing the full 25fps timeline through the proxy is pure
# wasted bandwidth, and the backlog is what makes lips drift behind the audio.
# Generation stays on the FPS timeline (pacing untouched) — we only skip sends.
SEND_FPS = int(os.environ.get("SEND_FPS", "0"))

# Cost controls (issue #3). Idle watchdog: if no page is connected for this many
# minutes, run GPU_SHUTDOWN_CMD (EBS-backed EC2: shutdown -h == STOP, meter off).
# 0 disables it — the local/dev default; setup.sh sets it on the real box.
IDLE_SHUTDOWN_MINUTES = int(os.environ.get("GPU_IDLE_SHUTDOWN_MINUTES", "0"))
SHUTDOWN_CMD = os.environ.get("GPU_SHUTDOWN_CMD", "sudo shutdown -h now")
HOURLY_USD = float(os.environ.get("GPU_HOURLY_USD", "1.006"))

# ── metrics state: counters and timings ONLY — never transcript/audio/content ─
STARTED_AT = time.time()
CLIENTS = 0
IDLE_SINCE: float | None = STARTED_AT  # None while >=1 client is connected
FRAMES_SENT = 0
FRAME_TIMES: deque = deque(maxlen=240)     # rolling window -> actual fps
FIRST_FRAME_MS: deque = deque(maxlen=50)   # speak received -> first frame out

app = FastAPI()


def _note_frame() -> None:
    global FRAMES_SENT
    FRAMES_SENT += 1
    FRAME_TIMES.append(time.time())


async def _idle_watchdog() -> None:
    """Stops the box when nobody is watching. Second layer of the dead-man pair:
    launch.sh arms a hard TTL for the whole window; this catches the earlier
    'meeting ended / page crashed and nothing reconnected' case."""
    while True:
        await asyncio.sleep(30)
        if IDLE_SHUTDOWN_MINUTES <= 0 or IDLE_SINCE is None:
            continue
        idle_min = (time.time() - IDLE_SINCE) / 60
        if idle_min >= IDLE_SHUTDOWN_MINUTES:
            print(f"[gpu-avatar] no clients for {idle_min:.0f} min "
                  f"(limit {IDLE_SHUTDOWN_MINUTES}) — shutting down", flush=True)
            os.system(SHUTDOWN_CMD)
            return


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

    async def talk_frames(self, audio_mp3: bytes, emotion: str | None = None):
        """Stub 'talking': same idle loop for the clip's rough duration.

        Yields frames at FPS; duration is estimated from mp3 size (~4KB/s at
        32kbps mono — rough is fine, the page ends on talk_end anyway).
        `emotion` is accepted for interface parity and ignored (no face).
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

    async def talk_frames(self, audio_mp3: bytes, emotion: str | None = None):
        """Yield MuseTalk-generated JPEG frames for this audio clip.
        `emotion` is accepted for interface parity (MuseTalk is mouth-only)."""
        async for frame in self.pipeline.stream(audio_mp3, fps=FPS,
                                                jpeg_quality=JPEG_QUALITY):
            yield frame


class DittoEngine:
    """Real engine #2: Ditto (Ant Group) — lip-sync + head motion/expressions.

    Interface-compatible with StubEngine/MuseTalkEngine. The heavy imports
    happen in start() so the module loads on any machine. The pipeline keeps
    ONE StreamSDK alive (avatar registered once at warmup) and serves each
    speak clip as an async stream of JPEG frames — see ditto_adapter.py.
    """

    def __init__(self, image_path: str) -> None:
        self.image_path = image_path
        self.pipeline = None
        self._stub = StubEngine(image_path)  # idle frames while not talking

    async def start(self) -> None:
        # Deferred import: only exists on the GPU box (Dockerfile.ditto).
        from ditto_adapter import DittoPipeline  # noqa: PLC0415

        self.pipeline = DittoPipeline(self.image_path)
        await asyncio.to_thread(self.pipeline.warmup, JPEG_QUALITY)

    def next_idle_frame(self) -> bytes:
        return self._stub.next_idle_frame()

    async def talk_frames(self, audio_mp3: bytes, emotion: str | None = None):
        """Yield Ditto-generated JPEG frames for this audio clip. The emotion
        label (backend emotion.py) conditions Ditto's motion generator, so the
        whole face leans into the line — see ditto_adapter.stream()."""
        async for frame in self.pipeline.stream(audio_mp3, fps=FPS,
                                                jpeg_quality=JPEG_QUALITY,
                                                emotion=emotion):
            yield frame


_ENGINES = {"musetalk": MuseTalkEngine, "ditto": DittoEngine}
_ENGINE_CLS = _ENGINES.get(ENGINE, StubEngine)

# Multi-volto: REFERENCE_IMAGES="laura:/x/laura.jpg,cedric:/x/cedric.jpg"
# crea UN engine per avatar (ognuno col suo volto registrato; ~2.6GB VRAM
# l'uno con ditto). Senza quella env: un solo engine da REFERENCE_IMAGE,
# comportamento identico a prima. La connessione /stream sceglie con
# ?avatar_id=<id>; id sconosciuto o assente -> il primo (default).
_FACES: dict[str, str] = {}
for _pair in filter(None, os.environ.get("REFERENCE_IMAGES", "").split(",")):
    _aid, _, _path = _pair.partition(":")
    if _aid.strip() and _path.strip():
        _FACES[_aid.strip()] = _path.strip()
if not _FACES:
    # A single legacy REFERENCE_IMAGE is explicitly Laura's by default. It may
    # never silently become Cedric/another avatar's face.
    _FACES[os.environ.get("DEFAULT_AVATAR_ID", "laura")] = REFERENCE_IMAGE

engines: dict[str, object] = {aid: _ENGINE_CLS(path) for aid, path in _FACES.items()}
engine = next(iter(engines.values()))  # default + retrocompatibilità


@app.on_event("startup")
async def _warmup() -> None:
    for aid, eng in engines.items():
        await eng.start()
        print(f"[gpu-avatar] volto '{aid}' pronto", flush=True)
    asyncio.create_task(_idle_watchdog())
    print(f"[gpu-avatar] engine={ENGINE} fps={FPS} faces={list(engines)} "
          f"idle_shutdown={IDLE_SHUTDOWN_MINUTES}min", flush=True)


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({
        "ok": True, "engine": ENGINE, "fps": FPS,
        "faces": {aid: {"ready": True} for aid in engines},
    })


@app.get("/metrics")
def metrics() -> JSONResponse:
    """Operational numbers only (issue #3) — no transcript, no user content."""
    now = time.time()
    fps_actual = 0.0
    if len(FRAME_TIMES) >= 2:
        span = FRAME_TIMES[-1] - FRAME_TIMES[0]
        if span > 0:
            fps_actual = round((len(FRAME_TIMES) - 1) / span, 1)
    uptime_s = int(now - STARTED_AT)
    return JSONResponse({
        "uptime_s": uptime_s,
        "engine": ENGINE,
        "fps_target": FPS,
        "fps_actual": fps_actual,
        "clients": CLIENTS,
        "frames_sent": FRAMES_SENT,
        "first_frame_ms_last": round(FIRST_FRAME_MS[-1], 1) if FIRST_FRAME_MS else None,
        "first_frame_ms_avg": (round(sum(FIRST_FRAME_MS) / len(FIRST_FRAME_MS), 1)
                               if FIRST_FRAME_MS else None),
        "est_cost_usd": round(uptime_s / 3600 * HOURLY_USD, 3),
        "idle_shutdown_minutes": IDLE_SHUTDOWN_MINUTES,
        "idle_for_s": int(now - IDLE_SINCE) if IDLE_SINCE is not None else 0,
    })


@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    global CLIENTS, IDLE_SINCE
    await ws.accept()
    # Face identity is fail-closed. A requested avatar must have its own engine;
    # an unknown id never inherits the first/default face.
    aid = (
        ws.query_params.get("avatar_id", "")
        or os.environ.get("DEFAULT_AVATAR_ID", "laura")
    )
    if aid not in engines:
        await ws.send_text(json.dumps({
            "type": "face_unavailable", "avatar_id": aid,
            "available": sorted(engines),
        }))
        await ws.close(code=4404, reason="face_unavailable")
        return
    eng = engines[aid]
    CLIENTS += 1
    IDLE_SINCE = None
    await ws.send_text(json.dumps({"type": "hello", "mode": ENGINE, "fps": FPS,
                                   "face": aid}))
    # Queue entries carry a command epoch as well as an optional backend speech
    # generation. Stop increments the epoch, invalidating every queued/active
    # pre-stop clip; newer commands remain playable even without generation ids.
    speak_queue: asyncio.Queue = asyncio.Queue()
    command_epoch = 0
    cancel_before = float("-inf")

    async def reader() -> None:
        nonlocal command_epoch, cancel_before
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")
            generation = msg.get("generation_id")
            generation = generation if isinstance(generation, (int, float)) else None
            if kind == "stop":
                command_epoch += 1
                if generation is not None:
                    cancel_before = max(cancel_before, generation)
            elif kind == "speak" and msg.get("audio_b64"):
                await speak_queue.put((
                    base64.b64decode(msg["audio_b64"]),
                    msg.get("emotion"),
                    generation,
                    command_epoch,
                ))

    reader_task = asyncio.create_task(reader())
    frame_interval = 1.0 / FPS
    try:
        while True:
            try:
                audio, emotion, generation, item_epoch = speak_queue.get_nowait()
            except asyncio.QueueEmpty:
                audio = None

            if audio is not None:
                # A queued command that predates a stop is discarded before it
                # can emit talk_start or a frame.
                if item_epoch != command_epoch or (
                    generation is not None and generation <= cancel_before
                ):
                    continue
                t_speak = time.perf_counter()
                await ws.send_text(json.dumps({
                    "type": "talk_start", "generation_id": generation,
                }))
                first_frame = True
                cancelled = False
                send_ratio = min(1.0, SEND_FPS / FPS) if SEND_FPS else 1.0
                send_acc = 1.0  # the first frame always goes out
                n_native = 0  # position of this frame on the FPS-native timeline
                async for frame in eng.talk_frames(audio, emotion=emotion):
                    # Cancellation is observed between generated frames. This
                    # bounds stop latency to one frame and prevents late output.
                    if item_epoch != command_epoch or (
                        generation is not None and generation <= cancel_before
                    ):
                        cancelled = True
                        break
                    t0 = time.perf_counter()
                    send_acc += send_ratio
                    if send_acc >= 1.0:
                        send_acc -= 1.0
                        # Tag each kept frame with its native-timeline index so the
                        # page can lock presentation to the AUDIO clock (frame i
                        # belongs at audio second i/FPS) instead of newest-wins —
                        # which is what lets sub-realtime generation drift the mouth
                        # off the voice. Additive: a legacy page ignores this text
                        # and the binary stays a bare JPEG.
                        await ws.send_text(json.dumps({
                            "type": "frame", "i": n_native,
                            "generation_id": generation,
                        }))
                        await ws.send_bytes(frame)
                        _note_frame()
                    if first_frame:
                        FIRST_FRAME_MS.append((t0 - t_speak) * 1000)
                        first_frame = False
                    # keep real-time pacing even if generation is faster —
                    # a skipped frame still burns its slot on the timeline
                    delay = frame_interval - (time.perf_counter() - t0)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    n_native += 1  # advance one native-timeline slot per frame
                await ws.send_text(json.dumps({
                    "type": "talk_end", "generation_id": generation,
                    "cancelled": cancelled,
                }))
            else:
                await ws.send_bytes(eng.next_idle_frame())
                _note_frame()
                await asyncio.sleep(1.0 / SEND_FPS if SEND_FPS else frame_interval)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader_task.cancel()
        CLIENTS -= 1
        if CLIENTS <= 0:
            CLIENTS = 0
            IDLE_SINCE = time.time()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
