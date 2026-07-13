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
  DITTO_REPO           path of the cloned repo      (default /opt/ditto/repo)
  DITTO_CHECKPOINTS    path of the checkpoints dir  (default /opt/ditto/checkpoints)
  DITTO_CHUNK          override chunksize, "3,5,2"
  DITTO_EMO_INTENSITY  0..1 emotion strength (default 0.5) — full 1.0 overrides
                       lip articulation (closed-smile mid-syllable); 0 disables

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
import time

import numpy as np

_REPO = os.environ.get("DITTO_REPO", "/opt/ditto/repo")
_CKPT = os.environ.get("DITTO_CHECKPOINTS", "/opt/ditto/checkpoints")
_CHUNK = tuple(int(x) for x in os.environ.get("DITTO_CHUNK", "3,5,2").split(","))

_SR = 16000          # hubert sample rate
_FRAME = 640         # samples per 40ms hubert frame (25fps native)
_WINDOW_PAD = 80     # alignment pad Ditto's online examples add to the window

# Emotion label (backend emotion.py) -> Ditto `emo` index. Ditto's order,
# verified in its condition_handler.py: 0 Angry, 1 Disgust, 2 Fear, 3 Happy,
# 4 Neutral, 5 Sad, 6 Surprise, 7 Contempt. Unknown labels render neutral.
_EMO_FOR = {"neutral": 4, "happy": 3, "excited": 3, "serious": 4, "concerned": 5}

# How hard the emotion drives the face, 0..1. A FULL emotion row overrides lip
# articulation — the 2026-07-12 A/B (same audio, aligned frames) caught "happy"
# rendering a CLOSED smile mid-syllable where neutral had parted, articulating
# lips. Blending the emotion row with neutral keeps the expression as a tint
# the mouth can articulate through. 0 = always neutral, 1 = full (old behavior).
def _read_emo_intensity() -> float:
    """Parse DITTO_EMO_INTENSITY, clamped to [0,1]. Runs at import (inside
    DittoEngine.start); a typo'd env must NOT crash the server's whole startup —
    a wrong intensity beats a dead face. Falls back to the 0.5 default."""
    raw = os.environ.get("DITTO_EMO_INTENSITY", "0.5")
    try:
        return min(1.0, max(0.0, float(raw)))
    except ValueError:
        print(f"[ditto] bad DITTO_EMO_INTENSITY={raw!r} — using 0.5", flush=True)
        return 0.5


_EMO_INTENSITY = _read_emo_intensity()


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

        cfg = os.environ.get("DITTO_CFG_PKL") or os.path.join(
            _CKPT, "ditto_cfg", "v0.4_hubert_cfg_trt_online.pkl"
        )
        data_root = os.environ.get("DITTO_DATA_ROOT") or os.path.join(
            _CKPT, "ditto_trt_Ampere_Plus"
        )
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

        # DITTO_TIMING=1: per-stage probes to find the fps bottleneck. Wraps
        # wav2feat (pure hubert cost) — run_chunk minus wav2feat = queue
        # backpressure from downstream stages. Numbers only, no content (PII).
        if os.environ.get("DITTO_TIMING"):
            import time as _t
            _orig_w2f = self.sdk.wav2feat
            def _timed_w2f(*a, **k):
                t0 = _t.perf_counter()
                r = _orig_w2f(*a, **k)
                print(f"[timing] wav2feat {(_t.perf_counter()-t0)*1000:.0f}ms",
                      flush=True)
                return r
            self.sdk.wav2feat = _timed_w2f

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

    def _set_emotion(self, emotion: str | None) -> None:
        """Point Ditto's condition handler at this clip's mood.

        The emotion is one row of the motion generator's conditioning vector —
        pure numpy (a softmax over 8 labels), rebuilt here in microseconds. No
        engine or avatar re-registration is involved. Safe between utterances:
        the pipeline serves one clip at a time by design."""
        ch = getattr(self.sdk, "condition_handler", None)
        if ch is None or not getattr(ch, "use_emo", False):
            return
        idx = _EMO_FOR.get((emotion or "neutral").strip().lower(), 4)
        if idx == getattr(self, "_cur_emo", 4):
            return
        try:
            row = ch._parse_emo_seq(idx)  # [1, 8] softmax row
            if idx != 4 and _EMO_INTENSITY < 1.0:
                # Tint, don't override: mix with neutral so the mouth keeps
                # articulating through the expression (see _EMO_INTENSITY).
                neutral = ch._parse_emo_seq(4)
                row = _EMO_INTENSITY * row + (1.0 - _EMO_INTENSITY) * neutral
            ch.emo_lst = row
            ch.num_emo = 1
            ch.emo_seq = np.concatenate([ch.emo_lst] * ch.seq_frames, 0)
            self._cur_emo = idx
        except Exception as e:  # noqa: BLE001 — a wrong face beats a dead face
            print(f"[ditto] set_emotion fallita ({e}) — resto neutrale", flush=True)

    # ── one utterance -> stream of JPEG frames ──
    async def stream(self, audio_mp3: bytes, fps: int = 25, jpeg_quality: int = 82,
                     emotion: str | None = None):
        """Async generator: JPEG frames for this clip, produced while Ditto
        renders. server.py handles pacing (sleep-to-FPS) and talk_start/end."""
        if self.sdk is None:
            raise RuntimeError("DittoPipeline.warmup() not called")
        self.writer.jpeg_quality = jpeg_quality
        self._set_emotion(emotion)

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
            timing = bool(os.environ.get("DITTO_TIMING"))
            with self._lock:
                for off in range(0, len(pcm), hop):
                    chunk = padded[off: off + window]
                    if len(chunk) < window:
                        chunk = np.pad(chunk, (0, window - len(chunk)))
                    t0 = time.perf_counter() if timing else 0.0
                    self.sdk.run_chunk(chunk, chunksize=_CHUNK)
                    if timing:
                        print(f"[timing] run_chunk {(time.perf_counter()-t0)*1000:.0f}ms",
                              flush=True)

        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()

        # 25 native fps -> requested fps by frame skipping (25->15: keep 3/5)
        keep_every = 25 / max(1, fps)
        emitted = 0.0
        served = 0
        while served < n_expected:
            # Feeder finito -> la pipeline sta solo svuotando le code: attesa
            # corta, così la clip chiude ~1s dopo l'ULTIMO frame reale invece
            # di aspettare 10s un frame che non arriverà (l'off-by-one tra
            # n_expected e frame prodotti costava una coda muta di 10s a clip
            # — era LUI il "11fps" misurato, non la pipeline).
            timeout = 10.0 if feeder.is_alive() else 1.0
            try:
                frame = await asyncio.to_thread(self.writer.frames.get, True, timeout)
            except queue.Empty:
                break   # generation stalled/finished — end the clip gracefully
            served += 1
            emitted += 1.0
            if emitted >= keep_every:
                emitted -= keep_every
                yield frame
