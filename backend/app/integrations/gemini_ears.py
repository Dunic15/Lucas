"""Gemini ears; natural STT + end-of-turn detection on the live meeting audio.

Recall streams the meeting's mixed raw audio (s16le 16 kHz mono, base64) to
/ws/recall-audio (main.py). Each frame is forwarded verbatim to a Gemini Live
session (BidiGenerateContent on the GLOBAL Vertex websocket host; the
region-prefixed host rejects Live models). Gemini transcribes continuously
(inputTranscription) and its native VAD closes the turn; that end-of-turn is
the signal Deepgram endpointing only approximates.

Modes (settings.gemini_ears_mode):
  off; module inert; nothing here runs (today's exact behavior).
  shadow; sessions run against real meetings but record METRICS ONLY.
           Transcripts are PII: no content is ever logged or exposed; the
           status surface reports counts and timing exclusively.
  on: Gemini turns become the utterance source: each completed turn is
           synthesized into the exact transcript.data payload Recall sends and
           POSTed to our own /webhooks/recall (marker: "laura_ears") so the
           ENTIRE existing pipeline (echo, barge-in, MeetingState, gates,
           answers) runs untouched. The speaker is merged from the most recent
           overlapping Recall final (Gemini hears mixed audio and cannot
           attribute speakers). While an ears session is healthy the raw
           Recall finals are suppressed (they still feed the speaker ring);
           if the session dies, suppression lifts and Recall finals drive the
           meeting again; automatic failover, never a deaf avatar.
  reply; everything "on" does, plus TUTTO-GEMINI: the Live model also DRAFTS
           the spoken reply from the audio it heard (persona system prompt,
           ~150 tokens). The draft rides the synthesized payload
           ("laura_ears_reply") and main.py speaks IT; through the same gates
           and the same ElevenLabs voice; instead of calling the brain.
           Trade: the spike's instant conversational feel, but the draft is
           NOT grounded in the avatar's documents (no RAG). A/B against "on".

Latency note: this module lives OFF the spoken hot path in shadow mode and
adds one localhost POST in on mode. The Gemini reply is capped at 1 token
(we want the turn boundary + transcription, not its answer).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field

from ..config import settings

# Live API runs ONLY on the global host (verified 2026-07-14; the region host
# closes 1008 "was not found" for live models).
_GEMINI_WS_URL = (
    "wss://aiplatform.googleapis.com/ws/"
    "google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent"
)

# Speaker-merge window: a Gemini turn takes its speaker from the newest Recall
# utterance seen within this many seconds. Beyond it, attribution is unsafe.
_SPEAKER_WINDOW_S = 12.0
_RING_MAX = 16
# The relay is considered "delivering" for this long after its last turn POST.
# While active, raw Recall finals are suppressed; when it goes quiet (relay or
# Gemini died), suppression lifts and Recall drives the meeting again.
_RELAY_ACTIVE_WINDOW = 20.0
_AUDIO_QUEUE_MAX = 200  # ~20s of 100ms frames; drop-oldest beyond (never block)
_MAX_RECONNECTS = 5


_ENABLED_MODES = ("shadow", "on", "reply")


def enabled() -> bool:
    return settings.gemini_ears_mode.strip().lower() in _ENABLED_MODES


def mode() -> str:
    return settings.gemini_ears_mode.strip().lower()


def mode_enabled(m: str) -> bool:
    return (m or "").strip().lower() in _ENABLED_MODES


def mode_for_avatar(avatar_id: str) -> str:
    """Effective ears mode for ONE avatar. The dashboard per-avatar brain choice
    ("gemini"/"cerebras") wins; otherwise the global default. This is what makes
    each avatar's brain selectable at runtime with no redeploy:
      gemini   -> reply  (tutto-Gemini via the relay)
      cerebras -> off    (the normal Deepgram + brain path)
      unset    -> settings.gemini_ears_mode (the global default)
    """
    from .. import store

    choice = store.get_avatar_brain_mode(avatar_id) if avatar_id else None
    if choice == "gemini":
        return "reply"
    if choice == "cerebras":
        return "off"
    return mode()


def _model_path() -> str:
    loc = settings.vertex_location or "us-central1"
    # the live model is exposed under a region location in the model path even
    # though the websocket host is global; "global" also works.
    if loc == "global":
        loc = "us-central1"
    return (
        f"projects/{settings.vertex_project}/locations/{loc}"
        f"/publishers/google/models/{settings.vertex_live_model}"
    )


def _self_base() -> str:
    base = (settings.self_base_url or "").strip()
    if base:
        return base.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"


@dataclass
class _Metrics:
    started_at: float = 0.0
    connected: bool = False
    audio_frames: int = 0
    dropped_frames: int = 0
    turns: int = 0
    turn_chars: int = 0
    reply_chars: int = 0
    speaker_matched: int = 0
    speaker_unmatched: int = 0
    synthesized_finals: int = 0
    recall_finals_seen: int = 0
    reconnects: int = 0
    last_turn_at: float = 0.0
    last_error: str = ""  # error class/short reason only; never content


@dataclass
class EarsSession:
    bot_id: str
    capability: str
    avatar_name: str = "Laura"  # persona for reply mode (from the session's avatar)
    metrics: _Metrics = field(default_factory=_Metrics)
    # ring of recent Recall finals for speaker attribution: (ts, speaker)
    # plus the text length only; the text itself is not retained here.
    _ring: list[tuple[float, str]] = field(default_factory=list)
    _queue: asyncio.Queue | None = None
    _task: asyncio.Task | None = None
    _closed: bool = False
    # RELAY architecture (App Runner can't accept inbound WS, so the Gemini
    # session runs in a Cloudflare Worker; the backend keeps only this light
    # state). Set every time the relay POSTs a turn; drives suppression:
    # raw Recall finals are suppressed only while the relay is actively
    # delivering, and resume the instant it goes quiet (failover).
    relay_active_at: float = 0.0

    def relay_active(self) -> bool:
        return bool(self.relay_active_at) and (
            time.time() - self.relay_active_at < _RELAY_ACTIVE_WINDOW
        )

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._queue = asyncio.Queue(maxsize=_AUDIO_QUEUE_MAX)
            self.metrics.started_at = time.time()
            self._task = asyncio.create_task(self._run())

    def close(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self.metrics.connected = False

    @property
    def healthy(self) -> bool:
        return (
            not self._closed
            and self._task is not None
            and not self._task.done()
            and self.metrics.connected
        )

    # ── inputs ─────────────────────────────────────────────────────────

    def feed_audio(self, b64_pcm: str) -> None:
        """Queue one Recall audio frame (base64 s16le 16 kHz mono)."""
        q = self._queue
        if q is None or self._closed:
            return
        self.metrics.audio_frames += 1
        try:
            q.put_nowait(b64_pcm)
        except asyncio.QueueFull:
            # Never build backpressure against the meeting: drop the oldest.
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(b64_pcm)
            except asyncio.QueueFull:
                self.metrics.dropped_frames += 1

    def observe_recall(self, speaker: str) -> None:
        """Record a Recall final's speaker for turn attribution (no content)."""
        self.metrics.recall_finals_seen += 1
        self._ring.append((time.time(), speaker))
        if len(self._ring) > _RING_MAX:
            del self._ring[: len(self._ring) - _RING_MAX]

    # ── gemini session ─────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            import websockets
        except ImportError:
            self.metrics.last_error = "websockets library missing"
            return
        from .. import llm

        backoff = 1.0
        while not self._closed and self.metrics.reconnects <= _MAX_RECONNECTS:
            try:
                token = await asyncio.to_thread(llm._vertex_token)
                async with websockets.connect(
                    _GEMINI_WS_URL,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    max_size=None,
                    open_timeout=20,
                ) as ws:
                    await ws.send(json.dumps(self._setup_payload()))
                    first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    if "setupComplete" not in first:
                        raise RuntimeError("gemini setup rejected")
                    self.metrics.connected = True
                    print(
                        f"[ears] gemini session UP bot={self.bot_id[:8]} "
                        f"mode={mode()}",
                        flush=True,
                    )
                    backoff = 1.0
                    await asyncio.gather(
                        self._pump_audio(ws), self._pump_events(ws)
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                self.metrics.connected = False
                self.metrics.last_error = type(e).__name__
                self.metrics.reconnects += 1
                print(
                    f"[ears] gemini session error bot={self.bot_id[:8]}: "
                    f"{type(e).__name__} (reconnect {self.metrics.reconnects})",
                    flush=True,
                )
                if self._closed:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
        self.metrics.connected = False

    def _setup_payload(self) -> dict:
        if mode() == "reply":
            # Tutto-Gemini: the Live model also DRAFTS the spoken reply (it has
            # the audio in context, so the draft streams while the turn closes
            #; the spike's instant feel). The gate downstream still decides
            # whether the draft is ever spoken, and ElevenLabs speaks it.
            gen = {"responseModalities": ["TEXT"], "maxOutputTokens": 150}
            system = (
                f"Sei {self.avatar_name}, un'assistente che partecipa a una "
                "riunione di lavoro. Rispondi nella lingua della riunione, con "
                "frasi brevi e naturali, come al telefono. Se non sai una "
                "cosa, dillo brevemente. Non fare elenchi."
            )
            # When the supervised browser is on, she CAN open and show
            # connected tools (e.g. Asana) and web pages live on her tile -
            # never deny it. The action itself is triggered separately; here we
            # only stop the reply brain from wrongly saying "I can't".
            if settings.browser_meeting_trigger_enabled:
                system += (
                    " Puoi anche aprire e mostrare dal vivo strumenti connessi "
                    "come Asana e pagine web sul tuo schermo quando qualcuno te "
                    "lo chiede, e guidare passo passo: non dire mai che non "
                    "puoi navigare o mostrare Asana."
                )
        else:
            # Ears-only: TEXT modality with a 1-token cap; we consume the
            # *turn boundary* and the input transcription, not Gemini's answer.
            gen = {"responseModalities": ["TEXT"], "maxOutputTokens": 1}
            system = "Rispondi sempre e solo con: ."
        return {
            "setup": {
                "model": _model_path(),
                "generationConfig": gen,
                "systemInstruction": {"parts": [{"text": system}]},
                "inputAudioTranscription": {},
            }
        }

    async def _pump_audio(self, ws) -> None:
        assert self._queue is not None
        while True:
            b64 = await self._queue.get()
            await ws.send(
                json.dumps(
                    {
                        "realtimeInput": {
                            "mediaChunks": [
                                {"mimeType": "audio/pcm;rate=16000", "data": b64}
                            ]
                        }
                    }
                )
            )

    async def _pump_events(self, ws) -> None:
        acc: list[str] = []
        reply_acc: list[str] = []
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            done = self._handle_gemini_message(msg, acc, reply_acc)
            if done:
                utterance = "".join(acc).strip()
                reply = "".join(reply_acc).strip()
                acc.clear()
                reply_acc.clear()
                if reply in (".", ""):  # ears-only sentinel; not a real draft
                    reply = ""
                if utterance:
                    await self._on_turn(utterance, reply)

    def _handle_gemini_message(
        self, msg: dict, acc: list[str], reply_acc: list[str] | None = None
    ) -> bool:
        """Fold one server message into the accumulators; True on turn end."""
        sc = msg.get("serverContent") or {}
        it = (sc.get("inputTranscription") or {}).get("text")
        if it:
            acc.append(it)
        if reply_acc is not None:
            for part in (sc.get("modelTurn") or {}).get("parts", []):
                if "text" in part:
                    reply_acc.append(part["text"])
        return bool(sc.get("turnComplete"))

    # ── turn handling ──────────────────────────────────────────────────

    def _match_speaker(self) -> str:
        now = time.time()
        for ts, speaker in reversed(self._ring):
            if now - ts <= _SPEAKER_WINDOW_S:
                self.metrics.speaker_matched += 1
                return speaker
        self.metrics.speaker_unmatched += 1
        return ""

    async def _on_turn(self, utterance: str, reply: str = "") -> None:
        self.metrics.turns += 1
        self.metrics.turn_chars += len(utterance)
        self.metrics.reply_chars += len(reply)
        self.metrics.last_turn_at = time.time()
        if mode() not in ("on", "reply"):
            return  # shadow: metrics only; content goes nowhere
        speaker = self._match_speaker()
        if not speaker:
            # No safe attribution: let the (suppressed) Recall final own this
            # stretch of speech rather than mis-attributing it. The webhook
            # suppression checks synthesized turns first, so nothing is lost.
            return
        await self._post_synthesized_final(
            speaker, utterance, reply if mode() == "reply" else ""
        )

    async def _post_synthesized_final(
        self, speaker: str, text: str, reply: str = ""
    ) -> None:
        import httpx

        payload = {
            "event": "transcript.data",
            "laura_ears": True,  # marks the payload so it is never suppressed
            "data": {
                "bot": {"id": self.bot_id},
                "data": {
                    "words": [{"text": text}],
                    "participant": {"name": speaker},
                },
            },
        }
        if reply:
            # reply mode: the draft the Live model already generated from the
            # audio; main.py speaks THIS (via ElevenLabs) instead of calling
            # the brain, IF the gates decide the turn deserves an answer.
            payload["laura_ears_reply"] = reply
        url = f"{_self_base()}/webhooks/recall?cap={self.capability}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload)
            self.metrics.synthesized_finals += 1
        except Exception as e:  # noqa: BLE001
            self.metrics.last_error = f"synth-post:{type(e).__name__}"

    # ── status (PII-safe) ──────────────────────────────────────────────

    def status(self) -> dict:
        m = self.metrics
        return {
            "mode": mode(),
            "relay_active": self.relay_active(),  # CF relay delivering turns?
            "last_turn_ago_s": round(time.time() - m.last_turn_at, 1) if m.last_turn_at else None,
            "healthy": self.healthy,
            "connected": m.connected,
            "audio_frames": m.audio_frames,
            "dropped_frames": m.dropped_frames,
            "turns": m.turns,
            "avg_turn_chars": round(m.turn_chars / m.turns, 1) if m.turns else 0,
            "reply_chars": m.reply_chars,
            "speaker_matched": m.speaker_matched,
            "speaker_unmatched": m.speaker_unmatched,
            "synthesized_finals": m.synthesized_finals,
            "recall_finals_seen": m.recall_finals_seen,
            "reconnects": m.reconnects,
            "last_error": m.last_error,
            "uptime_s": round(time.time() - m.started_at, 1) if m.started_at else 0,
        }


# ── registry ───────────────────────────────────────────────────────────

_sessions: dict[str, EarsSession] = {}


def ensure_session(
    bot_id: str, capability: str, avatar_name: str = "Laura"
) -> EarsSession:
    s = _sessions.get(bot_id)
    if s is None or s._closed:
        s = EarsSession(
            bot_id=bot_id, capability=capability, avatar_name=avatar_name
        )
        _sessions[bot_id] = s
        s.start()
    return s


def get_session(bot_id: str) -> EarsSession | None:
    return _sessions.get(bot_id)


def stop_session(bot_id: str) -> None:
    s = _sessions.pop(bot_id, None)
    if s is not None:
        s.close()


def _ensure_state(bot_id: str, capability: str = "", avatar_name: str = "Laura") -> EarsSession:
    """Light per-bot state holder (ring + metrics + relay-active), NO local
    Gemini task; in the relay architecture the Gemini session runs in the
    Cloudflare Worker, and the backend only tracks attribution + suppression."""
    s = _sessions.get(bot_id)
    if s is None or s._closed:
        s = EarsSession(bot_id=bot_id, capability=capability, avatar_name=avatar_name)
        _sessions[bot_id] = s
    return s


def observe_recall_final(bot_id: str, speaker: str) -> None:
    # Auto-create the light state on the first Recall final so the ring exists
    # for later relay-turn attribution (the relay hears mixed audio and can't
    # attribute speakers itself).
    _ensure_state(bot_id).observe_recall(speaker)


def note_relay_turn(bot_id: str) -> None:
    """Mark that the Cloudflare relay just delivered a turn for this bot."""
    s = _ensure_state(bot_id)
    s.relay_active_at = time.time()
    s.metrics.turns += 1
    s.metrics.synthesized_finals += 1
    s.metrics.last_turn_at = time.time()


def attribute_speaker(bot_id: str) -> str:
    """Best-effort speaker for a relay turn, from this bot's Recall-final ring
    (most recent within the window). "" when nothing recent enough."""
    s = _sessions.get(bot_id)
    return s._match_speaker() if s is not None else ""


def last_ring_speaker(bot_id: str) -> str:
    """The most recent Recall-final speaker for this bot, ANY age; the relay's
    fallback so it attributes to a real human (after a pause) instead of
    inventing a phantom name that would pollute the roster."""
    s = _sessions.get(bot_id)
    if s is None or not s._ring:
        return ""
    return s._ring[-1][1]


def should_suppress_recall_final(
    bot_id: str, payload: dict, resolved_mode: str | None = None
) -> bool:
    """True when a RAW Recall final must be suppressed (ears authoritative).

    ``resolved_mode`` is the effective mode for THIS bot's avatar (from
    mode_for_avatar); callers pass it so the choice is per-avatar. Defaults to
    the global mode for backward compatibility.

    Synthesized payloads (marker "laura_ears") are never suppressed; they ARE
    the ears output. Suppression requires on/reply mode AND an active relay;
    the moment it goes quiet this returns False and Recall finals drive the
    meeting again (failover).
    """
    if resolved_mode is None:
        resolved_mode = mode()
    if resolved_mode not in ("on", "reply"):
        return False
    if payload.get("laura_ears"):
        return False
    s = _sessions.get(bot_id)
    # Relay architecture: "active" = the Cloudflare relay POSTed a turn within
    # the last _RELAY_ACTIVE_WINDOW seconds. If it goes quiet (relay down, or
    # Gemini dropped and Recall hasn't reconnected the audio WS), suppression
    # lifts and raw Recall finals drive the meeting again.
    return s is not None and s.relay_active()


def status() -> dict:
    return {
        "mode": mode(),
        "sessions": {bot_id: s.status() for bot_id, s in _sessions.items()},
    }
