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


def test_event_agent_said_lands_in_transcript(client, bearer):
    """Live bug 2026-07-24: under the EL runtime nothing recorded HIS words —
    the artifact lost every agent line. agent_said is the transcript path."""
    s = _el_session("bot_said")
    r = client.post(
        "/internal/voice-agent/event/cap-bot_said",
        headers=bearer,
        json={"type": "agent_said", "text": "Got it — I'll set that up once we wrap."},
    )
    assert r.json()["recorded"] is True
    agent_lines = [u for u in s.transcript if u.speaker_kind == "agent"]
    assert len(agent_lines) == 1
    assert "set that up" in agent_lines[0].text
    store.remove("bot_said")


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


# ── client tools relay ────────────────────────────────────────────────


def _tool(client, cap: str, name: str, params: dict, headers, call_id="tc_1"):
    return client.post(
        f"/internal/voice-agent/tool/{cap}",
        headers=headers,
        json={"tool_name": name, "parameters": params, "tool_call_id": call_id},
    )


def test_tool_requires_bearer_and_el_runtime(client, bearer):
    r = _tool(client, "nope", "get_meeting_context", {}, {})
    assert r.status_code == 401
    store.create("bot_tleg", "https://meet.example/tleg", "laura")
    store.register_recall_realtime_capability("bot_tleg", "cap-tleg")
    r = _tool(client, "cap-tleg", "get_meeting_context", {}, bearer)
    assert r.status_code == 403  # legacy sessions never serve agent tools
    store.remove("bot_tleg")


def test_tool_unknown_name_is_explicit_error(client, bearer):
    _el_session("bot_tun")
    r = _tool(client, "cap-bot_tun", "rm_rf_everything", {}, bearer)
    assert r.status_code == 400
    assert "unknown tool" in r.json()["error"]
    store.remove("bot_tun")


def test_tool_meeting_context_returns_roster_and_brief(client, bearer):
    s = _el_session("bot_tctx")
    s.integration = {"brief": "Alpina deck due Friday.", "meeting": {"purpose": "Weekly"}}
    r = _tool(client, "cap-bot_tctx", "get_meeting_context", {}, bearer)
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["purpose"] == "Weekly"
    assert "Alpina" in body["result"]["meeting_brief"]
    store.remove("bot_tctx")


def test_tool_queue_action_complete_is_idempotent(client, bearer):
    _el_session("bot_tq")
    params = {
        "summary": "create a task",
        "details": "task called Finish the Alpina deck for Ananth by Friday",
        "request_id": "req-1",
    }
    r1 = _tool(client, "cap-bot_tq", "queue_action", params, bearer).json()
    assert r1["ok"] is True and r1["result"]["status"] == "queued"
    assert r1["result"]["approval_required"] is True
    action_id = r1["result"]["action_id"]
    # The agent retrying the SAME request must not mint a second action.
    r2 = _tool(client, "cap-bot_tq", "queue_action", params, bearer, call_id="tc_2").json()
    assert r2["result"]["status"] == "already_queued"
    assert r2["result"]["action_id"] == action_id
    # Live simulation 2026-07-24: the agent may OMIT request_id and a retry
    # mints a new tool_call_id — identical content must still dedupe.
    no_id = {k: v for k, v in params.items() if k != "request_id"}
    r3 = _tool(client, "cap-bot_tq", "queue_action", no_id, bearer, call_id="tc_3").json()
    assert r3["result"]["status"] == "already_queued"
    assert r3["result"]["action_id"] == action_id
    store.remove("bot_tq")


def test_tool_queue_action_vague_asks_for_details(client, bearer):
    _el_session("bot_tv")
    r = _tool(
        client, "cap-bot_tv", "queue_action",
        {"summary": "send an email", "request_id": "req-2"}, bearer,
    ).json()
    assert r["result"]["status"] == "needs_details"
    assert "email_to" in r["result"]["missing"]
    store.remove("bot_tv")


def test_tool_knowledge_search_formats_chunks(client, bearer, monkeypatch):
    from app.api import voice_agent as va

    class _Hit:
        text = "SFF Studio runs a venture studio model with 41 companies."
        source = "sff_overview.md"
        section = "Fund"
        score = 0.9

    from app.brain import rag

    monkeypatch.setattr(rag, "retrieve", lambda *a, **k: [_Hit()])
    _el_session("bot_tk")
    r = _tool(
        client, "cap-bot_tk", "search_company_knowledge",
        {"query": "how many companies"}, bearer,
    ).json()
    assert r["result"]["found"] is True
    assert "41 companies" in r["result"]["chunks"][0]["text"]
    store.remove("bot_tk")


def test_tool_upcoming_meetings_reads_session_snapshot(client, bearer):
    """Runtime tool-parity (live 2026-07-24: 'I don't have access to your
    calendar' while the legacy runtime had the snapshot all along)."""
    s = _el_session("bot_cal")
    s.calendar_brief = "Tomorrow 15:00 — Pilot review with Ananth."
    r = _tool(client, "cap-bot_cal", "get_upcoming_meetings", {}, bearer).json()
    assert "Pilot review" in r["result"]["summary"]
    store.remove("bot_cal")


def test_tool_leave_meeting_schedules_disconnect(client, bearer, monkeypatch):
    """Live 2026-07-24: he SAID 'I'll step out now' but the bot stayed until
    a manual end — leaving must be a deterministic tool, not a hope."""
    left: list[str] = []
    monkeypatch.setattr(
        voice_agent_api, "_schedule_leave", lambda session: left.append(session.bot_id)
    )
    _el_session("bot_bye")
    r = _tool(client, "cap-bot_bye", "leave_meeting", {}, bearer).json()
    assert r["result"]["status"] == "leaving"
    assert left == ["bot_bye"]
    store.remove("bot_bye")


def test_tool_queue_action_note_names_approval_not_meeting_end(client, bearer):
    _el_session("bot_note")
    r = _tool(
        client, "cap-bot_note", "queue_action",
        {"summary": "create a task", "details": "task called Ship the pilot report"},
        bearer,
    ).json()
    assert "approv" in r["result"]["note"]
    assert "after the meeting" not in r["result"]["note"]
    store.remove("bot_note")


def test_tool_crash_returns_502_not_traceback(client, bearer, monkeypatch):
    from app.brain import rag

    def _boom(*a, **k):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(rag, "retrieve", _boom)
    _el_session("bot_tc")
    r = _tool(client, "cap-bot_tc", "search_company_knowledge", {"query": "x"}, bearer)
    assert r.status_code == 502
    assert r.json() == {"ok": False, "error": "RuntimeError"}
    store.remove("bot_tc")


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
    # Ladder: separate-participant streams first (no echo, true barge-in),
    # mixed half-duplex as the flag-off fallback, untouched originals last.
    assert labels[0].endswith("+voice-sep")
    assert any(l.endswith("+voice-agent") for l in labels)
    assert any("+voice" not in l for l in labels)  # plain fallbacks kept
    sep = attempts[0][1]["recording_config"]
    assert "audio_separate_raw" in sep and "audio_mixed_raw" not in sep
    assert any(
        ep.get("url") == "wss://cedric-voice.example/voice/cap123"
        and ep.get("events") == ["audio_separate_raw.data"]
        for ep in sep["realtime_endpoints"]
    )
    mixed = next(b for l, b in attempts if l.endswith("+voice-agent"))["recording_config"]
    assert "audio_mixed_raw" in mixed and "audio_separate_raw" not in mixed
    # Plain fallback attempts must NOT stream audio anywhere.
    plain = attempts[-1][1]["recording_config"]
    assert "audio_mixed_raw" not in plain and "audio_separate_raw" not in plain


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


def test_legacy_barge_in_disabled_while_agent_owns_voice(client, monkeypatch):
    """EL runtime: interruption is the agent's (native, on separate streams).
    The legacy stop flushed the page while EL kept streaming — fragments and
    dead air mid-answer (live 2026-07-24)."""
    stops: list[str] = []

    async def fake_stop(session, *a, **k):
        stops.append(session.bot_id)

    monkeypatch.setattr(main_module, "_make_avatar_stop", fake_stop)
    monkeypatch.setattr(main_module, "_should_barge_in", lambda *a, **k: True)
    s = _el_session("bot_nobarge")
    s.voice_agent_active = True
    _final(client, "bot_nobarge", "so about the quarterly plan we should")
    assert stops == []  # no legacy stop while the agent owns the voice
    s.voice_agent_active = False
    # Different words: an identical repeated final would hit the duplicate-
    # final dedupe and exit before the barge-in block.
    _final(client, "bot_nobarge", "and the budget review is another thing")
    assert stops  # legacy sessions keep the protection
    store.remove("bot_nobarge")
