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


def test_schedule_leave_actually_finalizes(monkeypatch):
    """Live 2026-07-25: three leave_meeting tool calls, ZERO finalizes — the
    unreferenced asyncio task was garbage-collected mid-sleep. The task must
    be strongly held and must really reach _finalize_session."""
    import asyncio

    finalized: list[tuple] = []

    async def fake_finalize(bot_id, **kw):
        finalized.append((bot_id, kw.get("source")))

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(main_module, "_finalize_session", fake_finalize)

    class _S:
        bot_id = "bot_gc"

    async def scenario():
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        voice_agent_api._schedule_leave(_S())
        assert voice_agent_api._leave_tasks  # strong reference held
        await asyncio.gather(*list(voice_agent_api._leave_tasks))

    asyncio.run(scenario())
    assert finalized == [("bot_gc", "agent_leave")]
    assert not voice_agent_api._leave_tasks  # done-callback cleaned up


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


def test_tool_search_web_uses_native_search(client, bearer, monkeypatch):
    """Live 2026-07-24: 'I don't have direct internet access' while the
    legacy runtime had Claude native web search all along."""
    from app.brain import llm

    monkeypatch.setattr(settings, "live_search_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(
        llm, "web_search", lambda *a, **k: "Recall.ai cut prices to $0.50/hr in 2026."
    )
    _el_session("bot_web")
    r = _tool(client, "cap-bot_web", "search_web", {"query": "recall pricing"}, bearer).json()
    assert r["result"]["found"] is True
    assert "0.50" in r["result"]["answer"]
    store.remove("bot_web")


def test_tool_search_web_honest_when_disabled(client, bearer, monkeypatch):
    monkeypatch.setattr(settings, "live_search_enabled", False)
    _el_session("bot_noweb")
    r = _tool(client, "cap-bot_noweb", "search_web", {"query": "x"}, bearer).json()
    assert r["result"]["available"] is False
    store.remove("bot_noweb")


def test_tool_queue_action_clarify_caps_at_two_then_queues(client, bearer):
    """Live 2026-07-24: 'I need the full email body' repeated four times
    verbatim. Third attempt must queue what we have instead of looping."""
    _el_session("bot_loop")
    vague = {"summary": "send an email", "request_id": "req-loop"}
    r1 = _tool(client, "cap-bot_loop", "queue_action", vague, bearer, "t1").json()
    r2 = _tool(client, "cap-bot_loop", "queue_action", vague, bearer, "t2").json()
    assert r1["result"]["status"] == r2["result"]["status"] == "needs_details"
    r3 = _tool(client, "cap-bot_loop", "queue_action", vague, bearer, "t3").json()
    assert r3["result"]["status"] == "queued_incomplete"
    assert r3["result"]["action_id"]
    assert "approval card" in r3["result"]["note"]
    store.remove("bot_loop")


def test_tool_capabilities_returns_structured_truth(client, bearer):
    _el_session("bot_capst")
    r = _tool(client, "cap-bot_capst", "get_available_actions", {}, bearer).json()
    assert "summary" in r["result"]
    assert isinstance(r["result"]["tools"], dict)
    store.remove("bot_capst")


def test_tool_action_continuity_amend_and_withdraw(client, bearer):
    """Live 2026-07-24 ('got the meeting subject' had nowhere to land): a
    correction must update the SAME card; 'cancel that' must withdraw it."""
    _el_session("bot_cont")
    q = _tool(
        client, "cap-bot_cont", "queue_action",
        {"summary": "create a meeting",
         "details": "meeting tomorrow at 3pm with Ananth",
         "request_id": "req-cont"},
        bearer,
    ).json()
    action_id = q["result"]["action_id"]

    listed = _tool(client, "cap-bot_cont", "get_pending_actions", {}, bearer).json()
    assert any(a["action_id"] == action_id for a in listed["result"]["actions"])

    amended = _tool(
        client, "cap-bot_cont", "amend_pending_action",
        {"new_text": "meeting tomorrow at 3pm with Ananth, subject: Alpina review"},
        bearer, "tc_am",
    ).json()
    assert amended["result"]["status"] == "amended"
    assert amended["result"]["action_id"] == action_id  # SAME card
    assert "Alpina review" in amended["result"]["action"]

    gone = _tool(client, "cap-bot_cont", "withdraw_pending_action", {}, bearer).json()
    assert gone["result"]["status"] == "withdrawn"
    assert gone["result"]["action_id"] == action_id
    store.remove("bot_cont")


def test_bootstrap_language_knob_localizes_greeting(client, bearer, monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k")
    monkeypatch.setattr(voice_agent_api.httpx, "Client", _FakeMintClient)
    monkeypatch.setattr(settings, "voice_agent_language", "it")
    _el_session("bot_it")
    r = client.get("/internal/voice-agent/bootstrap/cap-bot_it", headers=bearer)
    agent_over = r.json()["init"]["conversation_config_override"]["agent"]
    assert agent_over["language"] == "it"
    assert "Ciao a tutti" in agent_over["first_message"]
    store.remove("bot_it")


def test_avatar_language_overrides_the_global_knob(monkeypatch):
    """One global env var meant "Italian for Laura" was also "Italian for
    Cedric, in every org on the runtime" — so nobody could set it, and an
    Italian room ran English ASR."""
    from dataclasses import replace

    monkeypatch.setattr(settings, "voice_agent_language", "en")
    s = _el_session("bot_avlang")
    av = avatars.load("petra")
    payload = voice_agent_api.build_init_payload(s, replace(av, voice_agent_language="it"))
    agent_over = payload["conversation_config_override"]["agent"]
    assert agent_over["language"] == "it"
    assert "Ciao a tutti" in agent_over["first_message"]
    # Unset on the avatar → the global still decides (Cedric is unaffected).
    payload = voice_agent_api.build_init_payload(s, replace(av, voice_agent_language=""))
    assert payload["conversation_config_override"]["agent"]["language"] == "en"
    store.remove("bot_avlang")


def test_avatar_language_rejects_an_unprovisioned_code(tmp_path, monkeypatch):
    """An unsupported language code is not a degraded call, it is a dead one at
    second zero — so the loader drops anything the agent is not built for."""
    # settings.avatars_dir is a read-only computed property — repoint REPO_ROOT
    # (same trick as test_mission_and_tasks._point_avatars_dir).
    from app import config as _config

    monkeypatch.setattr(_config, "REPO_ROOT", tmp_path)
    root = tmp_path / "avatars"
    root.mkdir(exist_ok=True)
    for code, expected in (("klingon", ""), ("IT", "it"), ("", "")):
        folder = root / "tester"
        folder.mkdir(exist_ok=True)
        (folder / "avatar.yaml").write_text(
            "id: tester\nname: Tester\nrole: r\npersona_prompt: p\n"
            f"voice_agent_language: '{code}'\n"
        )
        avatars._load_cache.clear()
        assert avatars.load("tester").voice_agent_language == expected


def test_agent_config_enables_language_detection():
    """The agent already ships an Italian preset, but a preset is unreachable
    until the CONVERSATION language is Italian. The system tool is what makes
    her follow a room that code-switches — and it is off by default."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "create_meeting_agent.py"
    spec = importlib.util.spec_from_file_location("_cma", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    prompt = mod.build_payload("petra")["conversation_config"]["agent"]["prompt"]
    ld = (prompt.get("built_in_tools") or {}).get("language_detection")
    assert ld and ld["params"]["system_tool_type"] == "language_detection"
    # And NOT in `tools`: on this account `tools` is the read-back expansion of
    # `tool_ids`, so writing a system tool there replaces every CLIENT tool.
    assert not prompt.get("tools"), "system tools must never be written to `tools`"


def test_tool_queue_action_stamps_requesting_speaker(client, bearer):
    """Owner plan P2: every action records WHO asked for it — the bridge
    sends the voiced speaker, the card carries the provenance."""
    _el_session("bot_prov")
    r = client.post(
        "/internal/voice-agent/tool/cap-bot_prov",
        headers=bearer,
        json={
            "tool_name": "queue_action",
            "parameters": {
                "summary": "create a task",
                "details": "task called Review provenance due Monday",
                "request_id": "req-prov",
            },
            "tool_call_id": "tc_p1",
            "speaker": "Duccio Profeti",
        },
    ).json()
    assert r["result"]["status"] == "queued"
    s = store.get("bot_prov")
    item = next(i for i in s.queued_actions if i["action_id"] == r["result"]["action_id"])
    assert "requested by Duccio Profeti" in item["action"]
    store.remove("bot_prov")


def test_bootstrap_context_includes_meeting_link(client, bearer, monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k")
    monkeypatch.setattr(voice_agent_api.httpx, "Client", _FakeMintClient)
    _el_session("bot_link")
    r = client.get("/internal/voice-agent/bootstrap/cap-bot_link", headers=bearer)
    prompt = r.json()["init"]["conversation_config_override"]["agent"]["prompt"]["prompt"]
    assert "meeting_link" in prompt
    assert "https://meet.example/va" in prompt
    store.remove("bot_link")


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


# ── Meeting Director: strict multiparty gate (owner plan P1/P3, 2026-07-25) ──


def _final_from(client, bot_id: str, text: str, name: str, pid: int) -> dict:
    return client.post(
        "/webhooks/recall",
        json={
            "event": "transcript.data",
            "data": {
                "bot": {"id": bot_id},
                "data": {
                    "words": [{"text": w} for w in text.split()],
                    "participant": {"name": name, "id": pid},
                },
            },
        },
    ).json()


def _partial_from(client, bot_id: str, text: str, name: str = "Duccio", pid: int = 1) -> dict:
    return client.post(
        "/webhooks/recall",
        json={
            "event": "transcript.partial_data",
            "data": {
                "bot": {"id": bot_id},
                "data": {
                    "words": [{"text": w} for w in text.split()],
                    "participant": {"name": name, "id": pid},
                },
            },
        },
    ).json()


def _signals(monkeypatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr(
        voice_agent_api, "signal_relay", lambda session, payload: sent.append(payload)
    )
    return sent


def test_started_event_stamps_voice_capability(client, bearer):
    """Director signals need the raw capability; the bridge's own
    authenticated calls are where it legitimately appears."""
    s = _el_session("bot_capst")
    client.post(
        "/internal/voice-agent/event/cap-bot_capst",
        headers=bearer,
        json={"type": "started"},
    )
    assert s.voice_capability == "cap-bot_capst"
    store.remove("bot_capst")


def test_director_strict_mode_flips_on_second_human(client, monkeypatch):
    """1 human → open; a SECOND human (even via transcript) crosses the
    threshold and the bridge is told exactly once per crossing."""
    sent = _signals(monkeypatch)
    s = _el_session("bot_dm")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_dm"
    _final_from(client, "bot_dm", "hello everyone how are we", "Duccio", 1)
    assert not any(p.get("type") == "mode" and p.get("strict") for p in sent)
    assert s.voice_strict_mode is False
    _final_from(client, "bot_dm", "hi all sorry I am late", "Ananth", 2)
    assert {"type": "mode", "strict": True} in sent
    assert s.voice_strict_mode is True
    # No re-send while the count stays put.
    n = sum(1 for p in sent if p.get("type") == "mode")
    _final_from(client, "bot_dm", "so where were we on this", "Ananth", 2)
    assert sum(1 for p in sent if p.get("type") == "mode") == n
    store.remove("bot_dm")


def test_director_gate_opens_when_called_and_stamps_window(client, monkeypatch):
    sent = _signals(monkeypatch)
    s = _el_session("bot_gate")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_gate"
    _final(client, "bot_gate", "Cedric what do you think about this")
    opens = [p for p in sent if p.get("type") == "gate_open"]
    assert opens and opens[0]["speaker"] == "Duccio"
    assert s.voice_gate_opened_at > 0
    store.remove("bot_gate")


def test_director_gate_open_debounced_on_repeating_partials(client, monkeypatch):
    """Partials repeat the same growing text — the bridge must not be spammed
    (each gate_open replays the buffer)."""
    sent = _signals(monkeypatch)
    s = _el_session("bot_deb")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_deb"
    _partial_from(client, "bot_deb", "Cedric can you check")
    _partial_from(client, "bot_deb", "Cedric can you check the tasks")
    opens = [p for p in sent if p.get("type") == "gate_open"]
    assert len(opens) == 1  # second landed inside the debounce window
    assert s.voice_gate_opened_at > 0
    store.remove("bot_deb")


def test_director_partial_stop_signals_bridge_not_page_flush(client, monkeypatch):
    """'Cedric stop' on a partial: the kill goes to the bridge (dropResponse +
    page interrupt) — the legacy page flush alone left EL streaming fragments
    (live 2026-07-24)."""
    sent = _signals(monkeypatch)
    stops: list[str] = []

    async def fake_stop(session, *a, **k):
        stops.append(session.bot_id)

    monkeypatch.setattr(main_module, "_make_avatar_stop", fake_stop)
    s = _el_session("bot_pstop")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_pstop"
    r = _partial_from(client, "bot_pstop", "Cedric stop")
    assert r.get("stopped") is True
    assert {"type": "stop"} in sent
    assert stops == []  # no legacy flush while the agent owns the voice
    store.remove("bot_pstop")


def test_director_closes_the_gate_when_the_room_turns_to_someone_else(
    client, monkeypatch
):
    """The conversational lock keeps the floor open for the person she just
    answered — but the moment THEY turn to a colleague, she must drop out of
    the conversation on that sentence:

        "Cedric, which tasks are overdue?"   -> "Three."
        "Ananth, can you take two?"          -> gate_close

    Without this the follow-up window would leave her listening to (and able to
    answer) a question aimed squarely at another human."""
    sent = _signals(monkeypatch)
    s = _el_session("bot_gc")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_gc"
    _final_from(client, "bot_gc", "hi all sorry I am late", "Ananth", 2)
    _final(client, "bot_gc", "Cedric which tasks are overdue")
    assert any(p.get("type") == "gate_open" for p in sent)
    sent.clear()
    _final(client, "bot_gc", "Ananth can you take two of them")
    assert {"type": "gate_close"} in sent
    assert s.voice_gate_speaker == ""
    store.remove("bot_gc")


def test_director_gate_close_fires_on_the_partial_too(client, monkeypatch):
    """Same as above but a beat earlier — the partial beats the final by ~a
    second, and a second of her listening to someone else's turn is a second
    she can answer it in."""
    sent = _signals(monkeypatch)
    s = _el_session("bot_gcp")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_gcp"
    _final_from(client, "bot_gcp", "hi all sorry I am late", "Ananth", 2)
    _final(client, "bot_gcp", "Cedric which tasks are overdue")
    sent.clear()
    _partial_from(client, "bot_gcp", "Ananth can you take two of them")
    assert {"type": "gate_close"} in sent
    store.remove("bot_gcp")


def test_director_ordinary_chatter_does_not_close_the_gate(client, monkeypatch):
    """Only a clear VOCATIVE ends her turn. Merely mentioning a colleague
    ("Ananth will own the rollout") is normal meeting talk — closing on that
    would make the follow-up window unusable."""
    sent = _signals(monkeypatch)
    s = _el_session("bot_gcn")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_gcn"
    _final_from(client, "bot_gcn", "hi all sorry I am late", "Ananth", 2)
    _final(client, "bot_gcn", "Cedric which tasks are overdue")
    sent.clear()
    _final(client, "bot_gcn", "Ananth will own the rollout next week")
    assert not any(p.get("type") == "gate_close" for p in sent)
    store.remove("bot_gcn")


def test_director_gate_close_does_not_spam_the_bridge(client, monkeypatch):
    """Repeating partials of the same aside must not become a control storm."""
    sent = _signals(monkeypatch)
    s = _el_session("bot_gcs")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_gcs"
    _final_from(client, "bot_gcs", "hi all sorry I am late", "Ananth", 2)
    _final(client, "bot_gcs", "Cedric which tasks are overdue")
    sent.clear()
    _partial_from(client, "bot_gcs", "Ananth can you take")
    _partial_from(client, "bot_gcs", "Ananth can you take two")
    _final_from(client, "bot_gcs", "Ananth can you take two of them", "Duccio", 1)
    assert len([p for p in sent if p.get("type") == "gate_close"]) == 1
    store.remove("bot_gcs")


def test_director_handover_to_a_new_addresser_is_never_debounced(client, monkeypatch):
    """The bridge forwards only the GATE SPEAKER's voice. If a second person
    names her inside the 1.5s debounce, suppressing that signal would leave
    them talking into a gate opened for the first — an addressed turn dropped,
    which is the one failure strict mode must never have."""
    sent = _signals(monkeypatch)
    s = _el_session("bot_ho")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_ho"
    _partial_from(client, "bot_ho", "Cedric can you check the tasks", "Duccio", 1)
    _partial_from(client, "bot_ho", "Cedric what about the deadline", "Ananth", 2)
    opens = [p for p in sent if p.get("type") == "gate_open"]
    assert [p["speaker"] for p in opens] == ["Duccio", "Ananth"]
    assert s.voice_gate_speaker == "Ananth"
    store.remove("bot_ho")


def test_agent_speech_arms_the_echo_guard(client, bearer):
    """Rooms without headphones send her own voice back through every open mic,
    and Recall transcribes it as THAT PERSON talking. The echo guard keys off
    lines she is known to have spoken — which the legacy path stamps inside
    `_make_avatar_speak`, a function this runtime never calls. Un-armed, her
    own greeting (which says her name out loud) could wake her, open the
    Director gate on the echo, and have her answer herself."""
    from app import main as main_mod

    s = _el_session("bot_echo")
    said = "The deadline is Friday and Ananth owns the migration"
    client.post(
        "/internal/voice-agent/event/cap-bot_echo",
        headers=bearer,
        json={"type": "agent_said", "text": said},
    )
    assert main_mod._is_echo(s, said) is True
    assert main_mod._is_echo(s, "and Ananth owns the migration") is True
    assert main_mod._is_echo(s, "so what did we decide about hiring") is False
    store.remove("bot_echo")


def test_director_mode_is_re_asserted_so_a_lost_signal_heals(client, monkeypatch):
    """signal_relay is fire-and-forget over the network and the bridge's state
    lives in a Durable Object's memory. One dropped POST or one DO restart used
    to desync the gate for the WHOLE meeting, in the worst direction: the
    bridge open while the backend believes it is enforcing. The periodic
    re-assert bounds that to 30s."""
    import time as _time

    sent = _signals(monkeypatch)
    s = _el_session("bot_reassert")
    s.voice_agent_active = True
    s.voice_capability = "cap-bot_reassert"
    _final_from(client, "bot_reassert", "hello everyone how are we", "Duccio", 1)
    _final_from(client, "bot_reassert", "hi all sorry I am late", "Ananth", 2)
    assert {"type": "mode", "strict": True} in sent
    sent.clear()
    # Nothing changed: no spam.
    _final_from(client, "bot_reassert", "so where were we", "Ananth", 2)
    assert not any(p.get("type") == "mode" for p in sent)
    # …but a stale statement is restated.
    s.voice_mode_signalled_at = _time.time() - 31.0
    _final_from(client, "bot_reassert", "ok lets keep going", "Ananth", 2)
    assert {"type": "mode", "strict": True} in sent
    store.remove("bot_reassert")


def test_write_tools_locked_in_strict_mode_without_addressed_turn(client, bearer):
    """Owner spec: with ≥2 humans, no action from turns not addressed to him.
    Reads and leave_meeting stay open (leave is meter safety)."""
    s = _el_session("bot_lock")
    s.voice_strict_mode = True
    s.voice_gate_opened_at = 0.0
    r = _tool(
        client, "cap-bot_lock", "queue_action",
        {"summary": "send an email", "details": "email test to X"}, bearer,
    ).json()
    assert r["result"]["status"] == "not_authorized"
    r = _tool(client, "cap-bot_lock", "get_pending_actions", {}, bearer).json()
    assert "status" not in r["result"] or r["result"].get("status") != "not_authorized"
    store.remove("bot_lock")


def test_write_tools_flow_with_recent_addressed_turn(client, bearer):
    import time as _time

    s = _el_session("bot_auth")
    s.voice_strict_mode = True
    s.voice_gate_opened_at = _time.time()
    r = _tool(
        client, "cap-bot_auth", "queue_action",
        {"summary": "create a task", "details": "task called Follow up with Ananth"},
        bearer,
    ).json()
    assert r["result"]["status"] != "not_authorized"
    store.remove("bot_auth")


def test_leave_meeting_never_gated_by_strict_mode(client, bearer, monkeypatch):
    left: list[str] = []
    monkeypatch.setattr(
        voice_agent_api, "_schedule_leave", lambda session: left.append(session.bot_id)
    )
    s = _el_session("bot_lgate")
    s.voice_strict_mode = True
    s.voice_gate_opened_at = 0.0
    r = _tool(client, "cap-bot_lgate", "leave_meeting", {}, bearer).json()
    assert r["result"]["status"] == "leaving"
    assert left == ["bot_lgate"]
    store.remove("bot_lgate")


def test_signal_relay_posts_control_url_and_holds_task_ref(monkeypatch):
    """wss base → https /control/{cap}; fire-and-forget with a strong task
    reference (same GC pitfall as _leave_tasks)."""
    import asyncio

    posted: list[tuple[str, dict]] = []

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            posted.append((url, json))

    monkeypatch.setattr(voice_agent_api.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        settings, "voice_agent_relay_ws_base",
        "wss://cedric-voice.example.workers.dev",
    )

    class _S:
        voice_capability = "cap-xyz"
        voice_agent_active = True

    async def scenario():
        voice_agent_api.signal_relay(_S(), {"type": "mode", "strict": True})
        assert voice_agent_api._control_tasks  # strong reference held
        await asyncio.gather(*list(voice_agent_api._control_tasks))

    asyncio.run(scenario())
    assert posted == [
        (
            "https://cedric-voice.example.workers.dev/control/cap-xyz",
            {"type": "mode", "strict": True},
        )
    ]
    assert not voice_agent_api._control_tasks


def test_signal_relay_noop_without_capability_or_ownership(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        voice_agent_api.httpx, "AsyncClient",
        lambda *a, **k: calls.append(1),
    )

    class _NoCap:
        voice_capability = ""
        voice_agent_active = True

    class _NotActive:
        voice_capability = "cap-1"
        voice_agent_active = False

    voice_agent_api.signal_relay(_NoCap(), {"type": "stop"})
    voice_agent_api.signal_relay(_NotActive(), {"type": "stop"})
    assert calls == []


# ── the Asana board in the per-call prompt (live incident 2026-07-27) ──────
# On this runtime the board never reached the avatar: it rides session.
# memory_brief, whose only consumer is the legacy brain, which an EL session
# returns long before. She did not go quiet about it — her persona, her
# knowledge pack and the capability tool all say she has a snapshot — so the
# model filled the gap from a knowledge document describing an EXAMPLE company.
# These tests pin the two halves: the board reaches the prompt, and the prompt
# states exactly one rule about it.


def _board_prompt(snapshot: str | None) -> str:
    """The agent prompt build_init_payload would ship for this board."""
    s = _el_session("bot_board")
    if snapshot is not None:
        s.asana_snapshot = snapshot
    try:
        payload = voice_agent_api.build_init_payload(s, avatars.load("petra"))
        prompt = payload["conversation_config_override"]["agent"]["prompt"]["prompt"]
    finally:
        store.remove("bot_board")
    # The prompt is hand-wrapped, so assert on content, not on line breaks.
    return " ".join(prompt.split())


BOARD = (
    "(as of 2026-07-27)\n"
    "• YC Demo — Fall 2026 — 2 open task(s)\n"
    "   - Finalize dashboard animation · owner: Ananth · due 2026-07-27\n"
)


def test_board_snapshot_reaches_the_agent_prompt():
    prompt = _board_prompt(BOARD)
    assert "YC Demo — Fall 2026" in prompt, "the board never reached the agent"
    assert "Finalize dashboard animation" in prompt
    assert "owner: Ananth" in prompt


def test_with_a_board_the_prompt_forbids_answering_from_knowledge_docs():
    prompt = _board_prompt(BOARD)
    assert "asana_board_at_meeting_start" in prompt
    assert "NEVER answer them from your knowledge documents" in prompt
    # The contradictory blanket denial must be GONE: keeping both in one prompt
    # is what let the model pick the knowledge document.
    assert "CANNOT live-read inboxes, drives or task boards" not in prompt


def test_without_a_board_the_prompt_keeps_the_honest_denial():
    prompt = _board_prompt("")
    assert "asana_board_at_meeting_start" not in prompt
    assert "CANNOT live-read inboxes, drives or task boards" in prompt
    assert "NEVER answer them from your knowledge documents" not in prompt


def test_board_is_capped_like_every_other_context_value():
    # Measure the injected RUN, not a total count: the prompt's own prose
    # contains a few x characters and the two rule branches differ by one, so
    # counting every x would be a brittle assertion.
    import re

    longest = max(len(m) for m in re.findall(r"x+", _board_prompt("x" * 9000)))
    assert longest <= 2400, "an unbounded board would blow the prompt"


def test_italian_greeting_uses_the_avatars_own_name(monkeypatch):
    """An Italian call had Laura opening with "sono Cedric" — the greeting was
    hardcoded while Cedric was the only avatar on this runtime. The first
    sentence an avatar says must be her own name."""
    monkeypatch.setattr(settings, "voice_agent_language", "it")
    s = _el_session("bot_it")
    try:
        payload = voice_agent_api.build_init_payload(s, avatars.load("petra"))
        greeting = payload["conversation_config_override"]["agent"]["first_message"]
    finally:
        store.remove("bot_it")
    assert "Laura" in greeting
    assert "Cedric" not in greeting
