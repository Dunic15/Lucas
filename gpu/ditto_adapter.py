"""Ditto real-time adapter — bridges Ditto's online StreamSDK to gpu/server.py.

The engine seam in server.py expects a pipeline object with:
    warmup()                                  (blocking, called once via thread)
    stream(mp3_bytes, fps, jpeg_quality)      (async generator of JPEG bytes)

How this maps onto Ditto (antgroup/ditto-talkinghead):
  * ONE StreamSDK is created at warmup and the avatar is registered ONCE from
    the reference portrait (face detect + appearance extraction are the slow
    part — doing them per-utterance would blow the <2s first-frame budget).
  * Ditto's writer normally saves frames to an .mp4. We swap `sdk.writer` for
    a shim with the same call shape — `writer(frame_rgb, fmt="rgb")` — that
    JPEG-encodes and pushes to a queue, which `stream()` drains.
  * Audio: server.py hands us the TTS mp3; Ditto's hubert wants 16kHz mono
    f32. ffmpeg (static binary from imageio-ffmpeg) does the decode.
  * Chunking: hubert frames are 40ms (640 samples @16k). chunksize=(3,5,2)
    means each run_chunk sees 3 past + 5 current + 2 future frames (6480
    samples with the +80 alignment pad) and advances by 5 frames (3200
    samples) -> ~5 output frames per chunk at 25fps.

Uses the TRT "online" config (v0.4_hubert_cfg_trt_online.pkl) + the
ditto_trt_Ampere_Plus engines — needs an Ampere-or-newer GPU (3090 / A10G /
L40S / 4090). The pytorch checkpoints are offline-only; keep them for clip
generation, not live.

Env knobs:
  DITTO_REPO         path of the cloned repo      (default /opt/ditto/repo)
  DITTO_CHECKPOINTS  path of the checkpoints dir  (default /opt/ditto/checkpoints)
  DITTO_CHUNK        override chunksize, "3,5,2"

Expect a tuning session on the real GPU (chunk pacing, queue depths, fade
settings) — that was always the plan for launch day.
"""
from __future__ import annotations

import asyncio
import io
import os
import queue
import subprocess
import sys
import threading

import numpy as np

_REPO = os.environ.get("DITTO_REPO", "/opt/ditto/repo")
_CKPT = os.environ.get("DITTO_CHECKPOINTS", "/opt/ditto/checkpoints")
_CHUNK = tuple(int(x) for x in os.environ.get("DITTO_CHUNK", "3,5,2").split(","))

_SR = 16000          # hubert sample rate
_FRAME = 640         # samples per 40ms hubert frame (25fps native)
_WINDOW_PAD = 80     # alignment pad Ditto's online examples add to the window


def _ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — fall back to system ffmpeg
        return "ffmpeg"


def _mp3_to_pcm16k(mp3_bytes: bytes) -> np.ndarray:
    """mp3 (or any ffmpeg-readable audio) -> float32 mono PCM @16kHz."""
    proc = subprocess.run(
        [_ffmpeg_bin(), "-v", "error", "-i", "pipe:0",
         "-f", "f32le", "-ac", "1", "-ar", str(_SR), "pipe:1"],
        input=mp3_bytes, stdout=subprocess.PIPE, check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


class _QueueWriter:
    """Drop-in for Ditto's VideoWriterByImageIO: same call shape, but frames
    go to an in-memory queue as JPEG instead of an mp4 on disk."""

    def __init__(self, jpeg_quality: int) -> None:
        self.frames: "queue.Queue[bytes]" = queue.Queue(maxsize=256)
        self.jpeg_quality = jpeg_quality

    def __call__(self, frame_rgb, fmt: str = "rgb") -> None:
        from PIL import Image
        img = Image.fromarray(np.asarray(frame_rgb)[..., :3])
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=self.jpeg_quality)
        try:
            self.frames.put_nowait(buf.getvalue())
        except queue.Full:      # backpressure: drop oldest, keep newest
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            self.frames.put_nowait(buf.getvalue())

    def close(self) -> None:   # parity with the real writer
        return


class DittoPipeline:
    """Owns one long-lived StreamSDK; serves utterances one at a time."""

    def __init__(self, image_path: str) -> None:
        self.image_path = image_path
        self.sdk = None
        self.writer: _QueueWriter | None = None
        self._lock = threading.Lock()   # one utterance at a time per GPU

    # ── warmup: import, build SDK, register the avatar ONCE ──
    def warmup(self, jpeg_quality: int = 82) -> None:
        sys.path.insert(0, _REPO)
        from stream_pipeline_online import StreamSDK  # noqa: PLC0415

        cfg = os.path.join(_CKPT, "ditto_cfg", "v0.4_hubert_cfg_trt_online.pkl")
        data_root = os.path.join(_CKPT, "ditto_trt_Ampere_Plus")
        self.sdk = StreamSDK(cfg, data_root)

        # Ditto wants a png path + an output path (only used by the writer we
        # are about to replace).
        src = self.image_path
        if not src.lower().endswith(".png"):
            from PIL import Image
            png = "/tmp/ditto_reference.png"
            Image.open(src).convert("RGB").save(png)
            src = png
        self.sdk.setup(src, "/tmp/ditto_live_unused.mp4")

        # Swap the disk writer for the in-memory queue shim.
        self.writer = _QueueWriter(jpeg_quality)
        try:
            self.sdk.writer.close()
        except Exception:  # noqa: BLE001
            pass
        self.sdk.writer = self.writer

        # Prime the whole pipeline with a short silence so the first REAL
        # utterance doesn't pay cold-start costs.
        silence = np.zeros(_FRAME * sum(_CHUNK) + _WINDOW_PAD, dtype=np.float32)
        self.sdk.run_chunk(silence, chunksize=_CHUNK)
        self._drain(max_frames=_CHUNK[1], timeout_s=30)

    def _drain(self, max_frames: int, timeout_s: float) -> int:
        got = 0
        while got < max_frames:
            try:
                self.writer.frames.get(timeout=timeout_s)
                got += 1
            except queue.Empty:
                break
        return got

    # ── one utterance -> stream of JPEG frames ──
    async def stream(self, audio_mp3: bytes, fps: int = 25, jpeg_quality: int = 82):
        """Async generator: JPEG frames for this clip, produced while Ditto
        renders. server.py handles pacing (sleep-to-FPS) and talk_start/end."""
        if self.sdk is None:
            raise RuntimeError("DittoPipeline.warmup() not called")
        self.writer.jpeg_quality = jpeg_quality

        # Flush any stale frames a previous utterance's trailing pipeline work
        # left in the queue — serving them now would lag the lips behind the
        # audio (found live: back-to-back clips bled ~30 frames into each other).
        try:
            while True:
                self.writer.frames.get_nowait()
        except queue.Empty:
            pass

        pcm = await asyncio.to_thread(_mp3_to_pcm16k, audio_mp3)
        n_expected = max(1, len(pcm) // _FRAME)          # native 25fps frames

        past, cur, fut = _CHUNK
        window = _FRAME * (past + cur + fut) + _WINDOW_PAD
        hop = _FRAME * cur
        # left-pad with the "past" context, right-pad to a whole window
        padded = np.concatenate([np.zeros(_FRAME * past, dtype=np.float32), pcm])

        def _feed() -> None:
            with self._lock:
                for off in range(0, len(pcm), hop):
                    chunk = padded[off: off + window]
                    if len(chunk) < window:
                        chunk = np.pad(chunk, (0, window - len(chunk)))
                    self.sdk.run_chunk(chunk, chunksize=_CHUNK)

        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()

        # 25 native fps -> requested fps by frame skipping (25->15: keep 3/5)
        keep_every = 25 / max(1, fps)
        emitted = 0.0
        served = 0
        while served < n_expected:
            try:
                frame = await asyncio.to_thread(self.writer.frames.get, True, 10.0)
            except queue.Empty:
                break   # generation stalled/finished — end the clip gracefully
            served += 1
            emitted += 1.0
            if emitted >= keep_every:
                emitted -= keep_every
                yield frame
