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
    monkeypatch.setattr(settings, "ears_relay_ws_base", "wss://relay.example")
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
            "wss://relay.example/realtime/recall-audio/capsecret"
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
        gemini_ears,
        "ensure_session",
        lambda bot_id, cap, avatar_name="Laura": _StubSession(),
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

    async def _fake_post(speaker, text, reply=""):
        posted.append((speaker, text))

    monkeypatch.setattr(s, "_post_synthesized_final", _fake_post)
    asyncio.run(s._on_turn("qual è il prossimo passo"))
    assert posted == [("Alice", "qual è il prossimo passo")]


# ── suppression / failover ─────────────────────────────────────────────

def _healthy_session(bot_id="bot-1"):
    # Relay architecture: "healthy/active" = the CF relay POSTed a turn just now.
    s = gemini_ears.EarsSession(bot_id=bot_id, capability="c")
    s.relay_active_at = time.time()
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
    # Relay went quiet (relay/Gemini died mid-meeting): last turn is stale.
    s.relay_active_at = time.time() - (gemini_ears._RELAY_ACTIVE_WINDOW + 5)
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


# ── reply mode (tutto-Gemini) ──────────────────────────────────────────

def test_reply_mode_setup_uncaps_tokens_and_uses_persona(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    monkeypatch.setattr(settings, "vertex_project", "p")
    s = gemini_ears.EarsSession(bot_id="b", capability="c", avatar_name="Laura")
    setup = s._setup_payload()["setup"]
    assert setup["generationConfig"]["maxOutputTokens"] > 1
    assert "Laura" in setup["systemInstruction"]["parts"][0]["text"]
    # ears-only modes keep the 1-token sentinel
    monkeypatch.setattr(settings, "gemini_ears_mode", "on")
    setup = s._setup_payload()["setup"]
    assert setup["generationConfig"]["maxOutputTokens"] == 1


def test_handle_message_accumulates_model_reply():
    s = gemini_ears.EarsSession(bot_id="b", capability="c")
    acc: list[str] = []
    reply_acc: list[str] = []
    s._handle_gemini_message(
        {"serverContent": {"inputTranscription": {"text": "come va"}}},
        acc, reply_acc,
    )
    s._handle_gemini_message(
        {"serverContent": {"modelTurn": {"parts": [{"text": "Tutto bene, "}]}}},
        acc, reply_acc,
    )
    done = s._handle_gemini_message(
        {"serverContent": {"modelTurn": {"parts": [{"text": "grazie."}]},
                           "turnComplete": True}},
        acc, reply_acc,
    )
    assert done
    assert "".join(acc) == "come va"
    assert "".join(reply_acc) == "Tutto bene, grazie."


def test_reply_mode_turn_attaches_draft_to_synthesized_final(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    s = gemini_ears.EarsSession(bot_id="b", capability="c")
    s.observe_recall("Alice")
    posted = []

    async def _fake_post(speaker, text, reply=""):
        posted.append((speaker, text, reply))

    monkeypatch.setattr(s, "_post_synthesized_final", _fake_post)
    asyncio.run(s._on_turn("Laura ci sei", "Sì, sono qui."))
    assert posted == [("Alice", "Laura ci sei", "Sì, sono qui.")]
    assert s.metrics.reply_chars == len("Sì, sono qui.")


def test_on_mode_turn_never_forwards_the_draft(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "on")
    s = gemini_ears.EarsSession(bot_id="b", capability="c")
    s.observe_recall("Alice")
    posted = []

    async def _fake_post(speaker, text, reply=""):
        posted.append(reply)

    monkeypatch.setattr(s, "_post_synthesized_final", _fake_post)
    asyncio.run(s._on_turn("una domanda", "Risposta indesiderata"))
    assert posted == [""]  # on-mode: the brain answers, never the draft


def test_suppression_active_in_reply_mode(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    _healthy_session()
    assert gemini_ears.should_suppress_recall_final(
        "bot-1", {"event": "transcript.data"}
    )
    assert not gemini_ears.should_suppress_recall_final(
        "bot-1", {"event": "transcript.data", "laura_ears": True}
    )


# ── reply mode end-to-end through the webhook ──────────────────────────

def _reply_session(tmp_path, monkeypatch, bot_id="reply-bot"):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    s.addressed_once = True
    s.participant_event("Ben", 1, here=True)
    monkeypatch.setattr(settings, "deference_seconds", 0)
    monkeypatch.setattr(settings, "recall_api_key", "")
    monkeypatch.setattr(settings, "ack_enabled", False)
    return s


def test_reply_mode_speaks_gemini_draft_and_skips_the_brain(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    s = _reply_session(tmp_path, monkeypatch)

    def _brain_must_not_run(*a, **k):
        raise AssertionError("answer_question_stream must not be called in reply mode")

    monkeypatch.setattr(main_module, "answer_question_stream", _brain_must_not_run)
    spoken = []

    async def fake_speak(session, text, *, force, generation, prev, t0=None):
        spoken.append(text)
        return True

    monkeypatch.setattr(main_module, "_speak_with_audio", fake_speak)

    payload = {
        "event": "transcript.data",
        "laura_ears": True,
        "laura_ears_reply": "Certo, sono qui e vi ascolto.",
        "data": {
            "bot": {"id": s.bot_id},
            "data": {
                "words": [{"text": w} for w in "Laura ci sei ?".split()],
                "participant": {"name": "Ben", "id": 1},
            },
        },
    }

    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    body = json.loads(asyncio.run(main_module.recall_webhook(FakeRequest())).body)
    assert body.get("spoke") is True
    assert spoken == ["Certo, sono qui e vi ascolto."]
    store.remove(s.bot_id)


def test_on_mode_synthesized_final_still_uses_the_brain(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "on")
    s = _reply_session(tmp_path, monkeypatch, bot_id="on-bot")

    def _grounded_stream(*a, **k):
        if k.get("meta") is not None:
            k["meta"]["top_score"] = 0.9
        yield "Risposta grounded dal RAG."

    monkeypatch.setattr(main_module, "answer_question_stream", _grounded_stream)
    spoken = []

    async def fake_speak(session, text, *, force, generation, prev, t0=None):
        spoken.append(text)
        return True

    monkeypatch.setattr(main_module, "_speak_with_audio", fake_speak)

    payload = {
        "event": "transcript.data",
        "laura_ears": True,
        # a stray draft in ON mode must be ignored by main.py
        "laura_ears_reply": "Draft che NON va parlato",
        "data": {
            "bot": {"id": s.bot_id},
            "data": {
                "words": [{"text": w} for w in "Laura ci sei ?".split()],
                "participant": {"name": "Ben", "id": 1},
            },
        },
    }

    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    body = json.loads(asyncio.run(main_module.recall_webhook(FakeRequest())).body)
    assert body.get("spoke") is True
    assert spoken == ["Risposta grounded dal RAG."]
    store.remove(s.bot_id)


def test_audio_ws_accepts_capability_in_path(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "shadow")
    monkeypatch.setattr(
        store, "resolve_recall_realtime_capability",
        lambda cap: "bot-9" if cap == "pathcap" else None,
    )
    fed: list[str] = []

    class _StubSession:
        def feed_audio(self, b64):
            fed.append(b64)

    monkeypatch.setattr(
        gemini_ears,
        "ensure_session",
        lambda bot_id, cap, avatar_name="Laura": _StubSession(),
    )
    client = TestClient(main_module.app)
    with client.websocket_connect("/realtime/recall-audio/pathcap") as ws:
        ws.send_text(json.dumps({
            "event": "audio_mixed_raw.data",
            "data": {"data": {"buffer": "UEFUSA=="}},
        }))
    assert fed == ["UEFUSA=="]


# ── relay architecture (CF Worker) ─────────────────────────────────────

def test_note_relay_turn_marks_active_and_counts(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    gemini_ears.note_relay_turn("bot-relay")
    s = gemini_ears._sessions["bot-relay"]
    assert s.relay_active() is True
    assert s.metrics.turns == 1
    assert gemini_ears.should_suppress_recall_final(
        "bot-relay", {"event": "transcript.data"}
    )


def test_attribute_speaker_from_recall_ring():
    gemini_ears.observe_recall_final("bot-attr", "Ben")
    gemini_ears.observe_recall_final("bot-attr", "Sara")
    assert gemini_ears.attribute_speaker("bot-attr") == "Sara"
    assert gemini_ears.attribute_speaker("unknown-bot") == ""


def test_ears_config_requires_bearer(monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    monkeypatch.setattr(settings, "laura_api_token", "secret-token")
    client = TestClient(main_module.app)
    r = client.get("/internal/ears-config/anycap")  # no bearer
    assert r.status_code == 401
    r = client.get("/internal/ears-config/anycap", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_ears_config_returns_token_and_persona(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    monkeypatch.setattr(settings, "laura_api_token", "secret-token")
    monkeypatch.setattr(settings, "vertex_project", "proj-1")
    monkeypatch.setattr(settings, "vertex_live_model", "gemini-live-2.5-flash")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create("cfg-bot", "https://meet.google.com/abc-defg-hij", "laura")
    monkeypatch.setattr(
        store, "resolve_recall_realtime_capability",
        lambda cap: "cfg-bot" if cap == "goodcap" else None,
    )
    from app import llm
    monkeypatch.setattr(llm, "_vertex_token", lambda: "fake-vertex-token")
    client = TestClient(main_module.app)
    r = client.get("/internal/ears-config/goodcap", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["mode"] == "reply"
    assert body["bot_id"] == "cfg-bot"
    assert body["project"] == "proj-1"
    assert body["live_model"] == "gemini-live-2.5-flash"
    assert body["vertex_token"] == "fake-vertex-token"
    assert body["persona"]  # avatar name resolved
    # invalid capability -> 404
    r2 = client.get("/internal/ears-config/badcap", headers={"Authorization": "Bearer secret-token"})
    assert r2.status_code == 404
    store.remove("cfg-bot")


def test_webhook_relay_turn_attributes_speaker(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gemini_ears_mode", "reply")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create("rt-bot", "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    s.addressed_once = True
    s.participant_event("Ben", 1, here=True)
    monkeypatch.setattr(settings, "deference_seconds", 0)
    monkeypatch.setattr(settings, "recall_api_key", "")
    monkeypatch.setattr(settings, "ack_enabled", False)
    # seed the ring so the relay turn can be attributed
    gemini_ears.observe_recall_final("rt-bot", "Ben")

    def _brain_must_not_run(*a, **k):
        raise AssertionError("reply mode must speak the draft, not call the brain")

    monkeypatch.setattr(main_module, "answer_question_stream", _brain_must_not_run)
    spoken = []

    async def fake_speak(session, text, *, force, generation, prev, t0=None):
        spoken.append(text)
        return True

    monkeypatch.setattr(main_module, "_speak_with_audio", fake_speak)

    payload = {
        "event": "transcript.data",
        "laura_ears": True,
        "laura_ears_text": "Laura ci sei ?",
        "laura_ears_reply": "Sì, sono qui.",
        "data": {
            "bot": {"id": "rt-bot"},
            "data": {"words": [{"text": w} for w in "Laura ci sei ?".split()], "participant": {}},
        },
    }

    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    body = json.loads(asyncio.run(main_module.recall_webhook(FakeRequest())).body)
    assert body.get("spoke") is True
    assert spoken == ["Sì, sono qui."]
    # the relay turn marked the bot active -> a raw Recall final is now suppressed
    assert gemini_ears.should_suppress_recall_final("rt-bot", {"event": "transcript.data"})
    store.remove("rt-bot")
