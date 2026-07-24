"""Cedric ElevenLabs pilot — PR 2 audio-bridge backend seams.

Covers the relay-facing endpoints (bootstrap auth + signed URL + per-meeting
init payload, lifecycle events flipping voice ownership), the live-path
suppression gate (agent owns the voice; stop/leave stay on the legacy path),
and the Recall bot-creation attach (audio endpoint + page voice params).
Key-free: every ElevenLabs call is stubbed.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from app import avatars, ledger, recall_client, store  # noqa: E402
from app.api import voice_agent as voice_agent_api  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    importlib.reload(store)
    importlib.reload(ledger)
    return TestClient(main_module.app)


@pytest.fixture
def bearer(monkeypatch):
    monkeypatch.setattr(settings, "laura_api_token", "tok-test")
    return {"Authorization": "Bearer tok-test"}


def _el_session(bot_id: str = "bot_va") -> store.Session:
    s = store.create(bot_id, "https://meet.example/va", "cedric")
    s.conversation_runtime = "elevenlabs_agent"
    s.elevenlabs_agent_id = "agent_test_1"
    cap = f"cap-{bot_id}"
    store.register_recall_realtime_capability(bot_id, cap)
    return s


class _FakeMintResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"signed_url": "wss://api.elevenlabs.io/x?token=short-lived"}


class _FakeMintClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, *a, **k):
        return _FakeMintResponse()


# ── bootstrap ─────────────────────────────────────────────────────────


def test_bootstrap_requires_bearer(client, bearer):
    r = client.get("/internal/voice-agent/bootstrap/whatever")
    assert r.status_code == 401
    r = client.get(
        "/internal/voice-agent/bootstrap/whatever",
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_bootstrap_unknown_capability_404(client, bearer):
    r = client.get("/internal/voice-agent/bootstrap/nope", headers=bearer)
    assert r.status_code == 404


def test_bootstrap_legacy_session_disabled(client, bearer):
    s = store.create("bot_leg", "https://meet.example/leg", "laura")
    store.register_recall_realtime_capability("bot_leg", "cap-leg")
    assert s.conversation_runtime == "legacy"
    r = client.get("/internal/voice-agent/bootstrap/cap-leg", headers=bearer)
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "reason": "legacy runtime"}
    store.remove("bot_leg")


def test_bootstrap_without_api_key_disabled(client, bearer):
    _el_session("bot_nokey")
    r = client.get("/internal/voice-agent/bootstrap/cap-bot_nokey", headers=bearer)
    assert r.json() == {"enabled": False, "reason": "no api key"}
    store.remove("bot_nokey")


def test_bootstrap_mints_signed_url_and_init(client, bearer, monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k")
    monkeypatch.setattr(voice_agent_api.httpx, "Client", _FakeMintClient)
    s = _el_session("bot_ok")
    s.integration = {
        "brief": "Weekly sync. Ananth owns the Alpina deck.",
        "meeting": {"purpose": "Weekly sync"},
    }
    r = client.get("/internal/voice-agent/bootstrap/cap-bot_ok", headers=bearer)
    body = r.json()
    assert body["enabled"] is True
    assert body["bot_id"] == "bot_ok"
    assert body["signed_url"].startswith("wss://")
    init = body["init"]
    assert init["type"] == "conversation_initiation_client_data"
    agent_over = init["conversation_config_override"]["agent"]
    # The AGENT owns the greeting: its static first_message must NOT be
    # overridden away (and the legacy self-intro is skipped for EL sessions).
    assert "first_message" not in agent_over
    prompt = agent_over["prompt"]["prompt"]
    # Persona + pilot rules + the meeting context in an UNTRUSTED block.
    assert "BEGIN UNTRUSTED MEETING DATA" in prompt
    assert "Alpina deck" in prompt
    assert "never claim" in prompt.lower()
    persona = avatars.load("cedric").persona_prompt.strip()
    assert persona[:60] in prompt
    # Referenced-or-not, the declared dynamic variables are always present
    # (a referenced-but-missing one kills the conversation at second zero).
    assert init["dynamic_variables"]["avatar_name"] == "Cedric"
    store.remove("bot_ok")


# ── lifecycle events ──────────────────────────────────────────────────


def test_event_started_flips_voice_owner(client, bearer):
    s = _el_session("bot_ev1")
    assert s.voice_agent_active is False
    r = client.post(
        "/internal/voice-agent/event/cap-bot_ev1",
        headers=bearer,
        json={"type": "started"},
    )
    assert r.json()["voice_owner"] == "elevenlabs"
    assert s.voice_agent_active is True
    store.remove("bot_ev1")


def test_event_failed_falls_back_and_speaks_once(client, bearer, monkeypatch):
    lines: list[str] = []

    async def fake_speak(session, line, **kwargs):
        lines.append(line)
        return True

    monkeypatch.setattr(main_module, "_make_avatar_speak", fake_speak)
    s = _el_session("bot_ev2")
    s.voice_agent_active = True
    r = client.post(
        "/internal/voice-agent/event/cap-bot_ev2",
        headers=bearer,
        json={"type": "failed"},
    )
    assert r.json()["voice_owner"] == "legacy"
    assert s.voice_agent_active is False
    assert len(lines) == 1  # the room hears the seam, once

    # A failure while NOT active (pre-meeting) stays silent: the bot simply
    # joins on the legacy path.
    r = client.post(
        "/internal/voice-agent/event/cap-bot_ev2",
        headers=bearer,
        json={"type": "failed"},
    )
    assert len(lines) == 1
    store.remove("bot_ev2")


def test_event_closed_is_silent(client, bearer, monkeypatch):
    lines: list[str] = []

    async def fake_speak(session, line, **kwargs):
        lines.append(line)
        return True

    monkeypatch.setattr(main_module, "_make_avatar_speak", fake_speak)
    s = _el_session("bot_ev3")
    s.voice_agent_active = True
    client.post(
        "/internal/voice-agent/event/cap-bot_ev3",
        headers=bearer,
        json={"type": "closed"},
    )
    assert s.voice_agent_active is False
    assert lines == []  # normal end of meeting: no spoken seam
    store.remove("bot_ev3")


# ── live-path suppression ─────────────────────────────────────────────


def _final(client, bot_id: str, text: str) -> dict:
    return client.post(
        "/webhooks/recall",
        json={
            "event": "transcript.data",
            "data": {
                "bot": {"id": bot_id},
                "data": {
                    "words": [{"text": w} for w in text.split()],
                    "participant": {"name": "Duccio", "id": 1},
                },
            },
        },
    ).json()


def test_agent_owns_the_voice_no_legacy_answer(client, monkeypatch):
    s = _el_session("bot_sup")
    s.voice_agent_active = True
    r = _final(client, "bot_sup", "Cedric can you check the open tasks please")
    assert r == {"ok": True, "spoke": False, "voice_owner": "elevenlabs"}
    # Ingestion still happened: the line is in the transcript for the artifact.
    assert any("open tasks" in u.text for u in s.transcript)
    store.remove("bot_sup")


def test_stop_command_survives_suppression(client, monkeypatch):
    stops: list[str] = []

    async def fake_stop(session, *a, **k):
        stops.append(session.bot_id)

    monkeypatch.setattr(main_module, "_make_avatar_stop", fake_stop)
    s = _el_session("bot_stop")
    s.voice_agent_active = True
    r = _final(client, "bot_stop", "Cedric stop")
    assert r.get("voice_owner") != "elevenlabs"  # fell through to control path
    assert stops  # the stop actually fired
    store.remove("bot_stop")


def test_self_intro_skipped_for_el_runtime(client, monkeypatch):
    monkeypatch.setattr(settings, "self_introduce_on_join", True)
    s = _el_session("bot_intro")
    assert main_module.maybe_self_introduce(s) is False  # the agent greets
    store.remove("bot_intro")


def test_explicit_leave_falls_through_suppression(client, monkeypatch):
    """'Leave the meeting' with NO name must still reach the legacy leave
    guards while the agent owns the voice — meter safety (audit 2026-07-24:
    all unaddressed leave variants were dead under suppression)."""
    monkeypatch.setattr(main_module.recall_client, "leave_call", lambda b: None)
    monkeypatch.setattr(main_module.recall_client, "delete_bot", lambda b: None)
    monkeypatch.setattr(
        main_module.anam_client, "end_conversation", lambda c: None
    )
    s = _el_session("bot_xleave")
    s.voice_agent_active = True
    r = _final(client, "bot_xleave", "please leave the meeting now")
    assert r.get("voice_owner") != "elevenlabs"  # fell through to legacy guards
    store.remove("bot_xleave")


def test_ack_and_backchannel_gated_when_agent_owns_voice(client, monkeypatch):
    """The partial path must not speak legacy acks/backchannels over the
    agent's voice (audit 2026-07-24: live double-voice in a 1:1)."""
    lines: list[str] = []

    async def fake_speak(session, line, **kwargs):
        lines.append(line)
        return True

    monkeypatch.setattr(main_module, "_make_avatar_speak", fake_speak)
    monkeypatch.setattr(main_module, "_should_backchannel", lambda *a, **k: True)
    monkeypatch.setattr(main_module, "_in_opening_grace", lambda s: False)
    monkeypatch.setattr(settings, "backchannel_enabled", True)
    s = _el_session("bot_bc")
    s.voice_agent_active = True
    r = client.post(
        "/webhooks/recall",
        json={
            "event": "transcript.partial_data",
            "data": {
                "bot": {"id": "bot_bc"},
                "data": {
                    "words": [
                        {"text": w}
                        for w in "so the plan for next quarter is quite long".split()
                    ],
                    "participant": {"name": "Duccio", "id": 1},
                },
            },
        },
    ).json()
    assert r.get("partial") is True
    assert lines == []  # no legacy voice while the agent owns the floor
    store.remove("bot_bc")


def test_suppression_lifts_when_bridge_dies(client, monkeypatch):
    s = _el_session("bot_lift")
    s.voice_agent_active = False  # bridge dead (or never came up)
    r = _final(client, "bot_lift", "Cedric can you check the open tasks please")
    assert r.get("voice_owner") != "elevenlabs"  # legacy path answered/handled
    store.remove("bot_lift")


# ── Recall attach + page params ───────────────────────────────────────


def test_attempts_gain_voice_endpoint_first_with_fallbacks():
    attempts = recall_client._create_bot_attempts(
        "https://meet.google.com/x",
        "https://backend/talk?avatar_id=cedric&conversation_id=c1",
        None,
        "Cedric",
        "cap123",
        attach_voice_agent_url="wss://cedric-voice.example/voice/cap123",
    )
    labels = [label for label, _ in attempts]
    assert labels[0].endswith("+voice-agent")
    assert any(not l.endswith("+voice-agent") for l in labels)  # fallbacks kept
    voiced = attempts[0][1]["recording_config"]
    assert "audio_mixed_raw" in voiced
    assert any(
        ep.get("url") == "wss://cedric-voice.example/voice/cap123"
        and ep.get("events") == ["audio_mixed_raw.data"]
        for ep in voiced["realtime_endpoints"]
    )
    # Plain fallback attempts must NOT stream audio anywhere.
    plain = attempts[-1][1]["recording_config"]
    assert "audio_mixed_raw" not in plain


def test_create_bot_appends_voice_params_and_skips_ears(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    monkeypatch.setattr(settings, "voice_agent_relay_ws_base", "wss://cv.example")
    monkeypatch.setattr(settings, "recall_api_key", "rk")
    from app import config as app_config

    monkeypatch.setattr(app_config, "REPO_ROOT", tmp_path)
    avatars._load_cache.clear()
    d = tmp_path / "avatars" / "cedric"
    d.mkdir(parents=True)
    (d / "avatar.yaml").write_text(
        "id: cedric\nconversation_runtime: elevenlabs_agent\n"
        "elevenlabs_agent_id: agent_x\n"
    )
    captured: dict = {}

    class _Resp:
        status_code = 201

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "bot_new"}

    def fake_request(method, url, **kwargs):
        captured.setdefault("bodies", []).append(kwargs.get("json"))
        return _Resp()

    monkeypatch.setattr(recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(recall_client, "_request", fake_request)
    bot = recall_client.create_bot(
        "https://meet.google.com/y",
        "https://backend/talk?avatar_id=cedric&conversation_id=c9",
        avatar_id="cedric",
    )
    assert bot["id"] == "bot_new"
    body = captured["bodies"][0]
    page_url = body["output_media"]["camera"]["config"]["url"]
    assert "voice_ws=" in page_url and "voice_cap=" in page_url
    eps = body["recording_config"]["realtime_endpoints"]
    voice_eps = [e for e in eps if "/voice/" in str(e.get("url", ""))]
    assert voice_eps, "voice endpoint attached"
    # Exactly ONE system owns the meeting audio: no gemini-ears endpoint.
    assert not any("recall-audio" in str(e.get("url", "")) for e in eps)


def test_create_bot_without_relay_base_stays_plain(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "elevenlabs_agent_runtime_enabled", True)
    monkeypatch.setattr(settings, "recall_api_key", "rk")
    assert settings.voice_agent_relay_ws_base == ""
    from app import config as app_config

    monkeypatch.setattr(app_config, "REPO_ROOT", tmp_path)
    avatars._load_cache.clear()
    d = tmp_path / "avatars" / "cedric"
    d.mkdir(parents=True)
    (d / "avatar.yaml").write_text(
        "id: cedric\nconversation_runtime: elevenlabs_agent\n"
        "elevenlabs_agent_id: agent_x\n"
    )
    captured: dict = {}

    class _Resp:
        status_code = 201

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "bot_p"}

    def fake_request(method, url, **kwargs):
        captured.setdefault("bodies", []).append(kwargs.get("json"))
        return _Resp()

    monkeypatch.setattr(recall_client, "assert_ready", lambda: None)
    monkeypatch.setattr(recall_client, "_request", fake_request)
    recall_client.create_bot(
        "https://meet.google.com/z",
        "https://backend/talk?avatar_id=cedric&conversation_id=c10",
        avatar_id="cedric",
    )
    body = captured["bodies"][0]
    assert "voice_ws=" not in body["output_media"]["camera"]["config"]["url"]
    assert not any(
        "/voice/" in str(e.get("url", ""))
        for e in body["recording_config"]["realtime_endpoints"]
    )
