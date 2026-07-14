"""Gemini ears (GEMINI_EARS_MODE) — key-free, no network.

Covers the contract that matters before this ever runs in prod:
  * off (default) keeps the bot config byte-identical to today — the
    live-meeting contract is untouched.
  * shadow/on prepend ears-enabled bot attempts and KEEP the plain ones as
    fallback (a Recall 4xx on the audio config can never block the invite).
  * the audio websocket route rejects a missing/invalid capability.
  * turn assembly + speaker merge + suppression/failover logic.
  * the status surface exposes counts only — never transcript content.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app import gemini_ears, recall_client, store
from app.config import settings


def _attempt_bodies(**kw):
    return recall_client._create_bot_attempts(
        "https://meet.google.com/abc-defg-hij",
        "https://example.test/talk",
        join_at=None,
        realtime_capability="capsecret",
        **kw,
    )


# ── bot config ─────────────────────────────────────────────────────────

def test_off_mode_keeps_bot_config_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "off")
    for label, body in _attempt_bodies():
        assert "+gemini-ears" not in label
        assert "audio_mixed_raw" not in body["recording_config"]
        endpoints = body["recording_config"]["realtime_endpoints"]
        assert all(e.get("type") == "webhook" for e in endpoints)


def test_shadow_mode_prepends_eared_attempts_with_plain_fallback(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "shadow")
    monkeypatch.setattr(settings, "public_base_url", "https://prod.example")
    attempts = _attempt_bodies()
    eared = [(l, b) for l, b in attempts if l.endswith("+gemini-ears")]
    plain = [(l, b) for l, b in attempts if not l.endswith("+gemini-ears")]
    assert eared and plain and len(eared) == len(plain)
    # eared attempts come FIRST (preferred), plain ones remain as fallback
    assert attempts[0][0].endswith("+gemini-ears")
    assert attempts[-1][0] == plain[-1][0]
    for _, body in eared:
        rc = body["recording_config"]
        assert rc["audio_mixed_raw"] == {}
        audio_eps = [e for e in rc["realtime_endpoints"] if e["type"] == "websocket"]
        assert len(audio_eps) == 1
        assert audio_eps[0]["events"] == ["audio_mixed_raw.data"]
        assert audio_eps[0]["url"] == (
            "wss://prod.example/realtime/recall-audio?cap=capsecret"
        )
    for _, body in plain:
        assert "audio_mixed_raw" not in body["recording_config"]


# ── websocket route auth ───────────────────────────────────────────────

def test_audio_ws_rejects_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "off")
    client = TestClient(main_module.app)
    with pytest.raises(Exception):
        with client.websocket_connect("/realtime/recall-audio?cap=whatever"):
            pass


def test_audio_ws_rejects_bad_capability(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "shadow")
    monkeypatch.setattr(
        store, "resolve_recall_realtime_capability", lambda cap: None
    )
    client = TestClient(main_module.app)
    with pytest.raises(Exception):
        with client.websocket_connect("/realtime/recall-audio?cap=wrong"):
            pass


def test_audio_ws_feeds_frames_to_session(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "shadow")
    monkeypatch.setattr(
        store, "resolve_recall_realtime_capability",
        lambda cap: "bot-1" if cap == "goodcap" else None,
    )
    fed: list[str] = []

    class _StubSession:
        def feed_audio(self, b64):
            fed.append(b64)

    monkeypatch.setattr(
        gemini_ears, "ensure_session", lambda bot_id, cap: _StubSession()
    )
    client = TestClient(main_module.app)
    with client.websocket_connect("/realtime/recall-audio?cap=goodcap") as ws:
        ws.send_text(json.dumps({
            "event": "audio_mixed_raw.data",
            "data": {"data": {"buffer": "QUJD"}},
        }))
        ws.send_text(json.dumps({"event": "something.else"}))
        ws.send_text(json.dumps({
            "event": "audio_mixed_raw.data",
            "data": {"data": {"buffer": "REVG"}},
        }))
    # asserting AFTER the context exits: TestClient drains the server loop on
    # close, so all queued frames are guaranteed processed by now.
    assert fed == ["QUJD", "REVG"]


# ── turn assembly + speaker merge ──────────────────────────────────────

def test_turn_assembly_accumulates_transcription_until_turn_complete():
    s = gemini_ears.EarsSession(bot_id="b", capability="c")
    acc: list[str] = []
    assert not s._handle_gemini_message(
        {"serverContent": {"inputTranscription": {"text": "ciao "}}}, acc
    )
    assert not s._handle_gemini_message(
        {"serverContent": {"modelTurn": {"parts": [{"text": "."}]}}}, acc
    )
    assert s._handle_gemini_message(
        {"serverContent": {"inputTranscription": {"text": "a tutti"},
                           "turnComplete": True}}, acc
    )
    assert "".join(acc) == "ciao a tutti"


def test_speaker_merge_prefers_recent_recall_speaker():
    s = gemini_ears.EarsSession(bot_id="b", capability="c")
    s.observe_recall("Alice")
    s.observe_recall("Bob")
    assert s._match_speaker() == "Bob"
    assert s.metrics.speaker_matched == 1


def test_speaker_merge_refuses_stale_attribution(monkeypatch):
    s = gemini_ears.EarsSession(bot_id="b", capability="c")
    s._ring.append((time.time() - 60.0, "Alice"))  # too old to trust
    assert s._match_speaker() == ""
    assert s.metrics.speaker_unmatched == 1


def test_shadow_turn_records_metrics_and_posts_nothing(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "shadow")
    s = gemini_ears.EarsSession(bot_id="b", capability="c")
    posted = []
    monkeypatch.setattr(
        s, "_post_synthesized_final",
        lambda *a, **k: posted.append(1),
    )
    asyncio.run(s._on_turn("una frase di prova"))
    assert s.metrics.turns == 1
    assert s.metrics.turn_chars == len("una frase di prova")
    assert posted == []


def test_on_turn_without_attribution_stays_silent(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "on")
    s = gemini_ears.EarsSession(bot_id="b", capability="c")
    posted = []

    async def _fake_post(speaker, text):
        posted.append((speaker, text))

    monkeypatch.setattr(s, "_post_synthesized_final", _fake_post)
    asyncio.run(s._on_turn("frase senza speaker"))
    assert posted == []  # no safe speaker -> the Recall final owns this speech


def test_on_turn_with_attribution_synthesizes_final(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "on")
    s = gemini_ears.EarsSession(bot_id="b", capability="c")
    s.observe_recall("Alice")
    posted = []

    async def _fake_post(speaker, text):
        posted.append((speaker, text))

    monkeypatch.setattr(s, "_post_synthesized_final", _fake_post)
    asyncio.run(s._on_turn("qual è il prossimo passo"))
    assert posted == [("Alice", "qual è il prossimo passo")]


# ── suppression / failover ─────────────────────────────────────────────

def _healthy_session(bot_id="bot-1"):
    s = gemini_ears.EarsSession(bot_id=bot_id, capability="c")
    s.metrics.connected = True

    class _FakeTask:
        def done(self):
            return False

        def cancel(self):
            return None

    s._task = _FakeTask()
    gemini_ears._sessions[bot_id] = s
    return s


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    gemini_ears._sessions.clear()


def test_suppression_only_in_on_mode_with_healthy_session(monkeypatch):
    payload = {"event": "transcript.data"}
    monkeypatch.setattr(settings, "gemini_ears_mode", "shadow")
    _healthy_session()
    assert not gemini_ears.should_suppress_recall_final("bot-1", payload)
    monkeypatch.setattr(settings, "gemini_ears_mode", "on")
    assert gemini_ears.should_suppress_recall_final("bot-1", payload)


def test_synthesized_payload_is_never_suppressed(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "on")
    _healthy_session()
    assert not gemini_ears.should_suppress_recall_final(
        "bot-1", {"event": "transcript.data", "laura_ears": True}
    )


def test_dead_session_fails_over_to_recall(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "on")
    s = _healthy_session()
    s.metrics.connected = False  # gemini WS died mid-meeting
    assert not gemini_ears.should_suppress_recall_final(
        "bot-1", {"event": "transcript.data"}
    )


# ── PII safety ─────────────────────────────────────────────────────────

def test_status_exposes_counts_never_content(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "shadow")
    s = gemini_ears.EarsSession(bot_id="bot-1", capability="c")
    gemini_ears._sessions["bot-1"] = s
    asyncio.run(s._on_turn("questa frase è PII e non deve uscire"))
    s.observe_recall("Alice")
    blob = json.dumps(gemini_ears.status())
    assert "PII" not in blob and "frase" not in blob
    assert "Alice" not in blob  # speaker names stay out of telemetry too
    st = gemini_ears.status()["sessions"]["bot-1"]
    assert st["turns"] == 1 and st["recall_finals_seen"] == 1


# ── vertex SA-from-env auth ────────────────────────────────────────────

def test_vertex_token_rejects_malformed_sa_json(monkeypatch):
    from app import llm

    monkeypatch.setattr(llm, "_vertex_token_cache", {"tok": "", "exp": 0.0})
    monkeypatch.setattr(settings, "google_vertex_sa_json", "{not json")
    with pytest.raises(RuntimeError, match="GOOGLE_VERTEX_SA_JSON"):
        llm._vertex_token()
