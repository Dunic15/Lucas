"""The three live pilot meetings (2026-07-24/25), replayed as regressions.

Each class recreates ONE live meeting's failure modes as a deterministic
scenario against the webhook + Director + tool seams. Lines are SYNTHETIC
recreations of what went wrong (real transcripts are PII and never enter the
repo) — the shape of the bug is what's under test, not the words.

Meeting A (1:1, uii-haza-bpg): invented email recipient, leave lag.
Meeting B (1:1, deadline call): answered a date from stale memory.
Meeting C (multiparty, Ananth): spoke into human-to-human talk, actions from
turns not addressed to him — the meeting that forced the strict gate.
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from app import ledger, store  # noqa: E402
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


def _el_session(bot_id: str) -> store.Session:
    s = store.create(bot_id, "https://meet.example/regression", "cedric")
    s.conversation_runtime = "elevenlabs_agent"
    s.elevenlabs_agent_id = "agent_test_1"
    s.voice_agent_active = True
    s.voice_capability = f"cap-{bot_id}"
    store.register_recall_realtime_capability(bot_id, f"cap-{bot_id}")
    return s


def _say(client, bot_id: str, text: str, name: str, pid: int, *, partial=False):
    return client.post(
        "/webhooks/recall",
        json={
            "event": (
                "transcript.partial_data" if partial else "transcript.data"
            ),
            "data": {
                "bot": {"id": bot_id},
                "data": {
                    "words": [{"text": w} for w in text.split()],
                    "participant": {"name": name, "id": pid},
                },
            },
        },
    ).json()


def _tool(client, cap, name, params, headers, call_id="tc_reg"):
    return client.post(
        f"/internal/voice-agent/tool/{cap}",
        headers=headers,
        json={"tool_name": name, "parameters": params, "tool_call_id": call_id},
    )


def _signals(monkeypatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr(
        voice_agent_api, "signal_relay", lambda session, payload: sent.append(payload)
    )
    return sent


# ── Meeting C — multiparty (the one that forced the strict gate) ──────


class TestMultipartyMeeting:
    def test_human_to_human_talk_never_opens_the_gate(self, client, monkeypatch):
        """Two humans discussing among themselves: strict mode must engage on
        the second voice and NO gate_open may fire — in the live meeting
        Cedric replied into their conversation repeatedly."""
        sent = _signals(monkeypatch)
        s = _el_session("bot_regC1")
        _say(client, "bot_regC1", "so I think the rollout plan needs another week", "Dana", 1)
        _say(client, "bot_regC1", "agreed and the budget side is still open", "Arun", 2)
        _say(client, "bot_regC1", "yeah okay let us circle back on that", "Dana", 1)
        assert {"type": "mode", "strict": True} in sent
        assert not [p for p in sent if p.get("type") == "gate_open"]
        store.remove("bot_regC1")

    def test_addressed_turn_opens_gate_for_the_addresser_only(self, client, monkeypatch):
        sent = _signals(monkeypatch)
        s = _el_session("bot_regC2")
        _say(client, "bot_regC2", "hello everyone", "Dana", 1)
        _say(client, "bot_regC2", "hi there", "Arun", 2)
        _say(client, "bot_regC2", "Cedric can you list the pending actions", "Arun", 2)
        opens = [p for p in sent if p.get("type") == "gate_open"]
        assert opens and opens[-1]["speaker"] == "Arun"
        assert s.voice_gate_opened_at > 0
        store.remove("bot_regC2")

    def test_no_actions_from_turns_not_addressed_to_him(self, client, bearer, monkeypatch):
        """Live: an action was queued off a sentence two humans exchanged.
        While strict with no addressed turn, a write tool call (whatever the
        model hallucinates) must come back not_authorized."""
        _signals(monkeypatch)
        s = _el_session("bot_regC3")
        _say(client, "bot_regC3", "we should email the deck to the investors", "Dana", 1)
        _say(client, "bot_regC3", "yes definitely go ahead with that", "Arun", 2)
        assert s.voice_strict_mode is True
        r = _tool(
            client, "cap-bot_regC3", "queue_action",
            {"summary": "send an email", "details": "email the deck to investors"},
            bearer,
        ).json()
        assert r["result"]["status"] == "not_authorized"
        store.remove("bot_regC3")

    def test_anothers_yes_does_not_extend_an_expired_window(self, client, bearer, monkeypatch):
        """A bystander's 'yes' after the 90s authorization window must not
        let a write through — every new turn re-requires the name."""
        _signals(monkeypatch)
        s = _el_session("bot_regC4")
        _say(client, "bot_regC4", "hello", "Dana", 1)
        _say(client, "bot_regC4", "Cedric queue a task for the report", "Arun", 2)
        assert s.voice_gate_opened_at > 0
        s.voice_gate_opened_at = time.time() - 300  # window long gone
        _say(client, "bot_regC4", "yes sounds good", "Dana", 1)  # not addressed
        r = _tool(
            client, "cap-bot_regC4", "queue_action",
            {"summary": "create a task", "details": "task called Report"},
            bearer,
        ).json()
        assert r["result"]["status"] == "not_authorized"
        store.remove("bot_regC4")


# ── Meeting A — 1:1: invented recipient, leave lag ─────────────────────


class TestOneOnOneMeeting:
    def test_one_on_one_stays_out_of_strict_mode(self, client, monkeypatch):
        """A single human must keep today's fluid 1:1 (no name needed) —
        strict never engages, writes stay open."""
        sent = _signals(monkeypatch)
        s = _el_session("bot_regA1")
        _say(client, "bot_regA1", "okay let us plan the week", "Duccio", 1)
        _say(client, "bot_regA1", "first the pilot report then the deck", "Duccio", 1)
        assert s.voice_strict_mode is False
        assert not any(p.get("type") == "mode" for p in sent)
        store.remove("bot_regA1")

    def test_queued_email_carries_the_requesting_speaker(self, client, bearer):
        """Live: 'email it to Anant' minted an INVENTED recipient with no
        provenance. The queue path must stamp who asked."""
        _el_session("bot_regA2")
        r = _tool(
            client, "cap-bot_regA2", "queue_action",
            {"summary": "send an email", "details": "email the summary to the team"},
            bearer,
        )
        body = r.json()
        assert body["ok"] is True
        # provenance rides the capture; the speaker comes from the bridge
        r2 = client.post(
            "/internal/voice-agent/tool/cap-bot_regA2",
            headers=bearer,
            json={
                "tool_name": "queue_action",
                "parameters": {
                    "summary": "send an email",
                    "details": "email the summary to the whole team today",
                },
                "tool_call_id": "tc_sp",
                "speaker": "Duccio",
            },
        ).json()
        assert r2["ok"] is True
        store.remove("bot_regA2")

    def test_called_leave_with_spoken_commas_finalizes(self, client, monkeypatch):
        """Live: 'can you leave the call, please?' was refused (regex missed
        the spoken comma) and later leaves lagged. The called leave must fall
        through suppression and reach finalize."""
        finalized: list[str] = []

        async def fake_finalize(bot_id, **kw):
            finalized.append(bot_id)
            return None

        monkeypatch.setattr(main_module, "_finalize_session", fake_finalize)
        monkeypatch.setattr(
            main_module.recall_client, "leave_call", lambda b: None
        )
        _signals(monkeypatch)
        s = _el_session("bot_regA3")
        r = _say(
            client, "bot_regA3",
            "Cedric can you leave the call, please?", "Duccio", 1,
        )
        assert r.get("voice_owner") != "elevenlabs"  # fell through the gate
        store.remove("bot_regA3")

    def test_stop_on_partial_kills_at_the_bridge(self, client, monkeypatch):
        sent = _signals(monkeypatch)
        stops: list[str] = []

        async def fake_stop(session, *a, **k):
            stops.append(session.bot_id)

        monkeypatch.setattr(main_module, "_make_avatar_stop", fake_stop)
        _el_session("bot_regA4")
        r = _say(client, "bot_regA4", "Cedric stop", "Duccio", 1, partial=True)
        assert r.get("stopped") is True
        assert {"type": "stop"} in sent
        assert stops == []
        store.remove("bot_regA4")


# ── Meeting B — dates come from search, not memory ─────────────────────


class TestDeadlineMeeting:
    def test_search_web_tool_is_wired_not_refused(self, client, bearer, monkeypatch):
        """Live: he answered the YC deadline from stale memory. The fix is a
        real search_web tool — the seam must call the live search, and its
        honest 'disabled' path must never fabricate."""
        from app.brain import llm

        calls: list[str] = []

        def fake_search(system, query, **kw):
            calls.append(query)
            return "July 27, 2026"

        monkeypatch.setattr(llm, "web_search", fake_search)
        monkeypatch.setattr(settings, "live_search_enabled", True)
        monkeypatch.setattr(settings, "anthropic_api_key", "key-test")
        _el_session("bot_regB1")
        r = _tool(
            client, "cap-bot_regB1", "search_web",
            {"query": "YC Fall 2026 application deadline"}, bearer,
        ).json()
        assert calls and "deadline" in calls[0]
        assert "2026" in str(r["result"])
        store.remove("bot_regB1")
