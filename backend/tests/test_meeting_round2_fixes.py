"""Round-2 live-meeting fixes (owner test 2026-07-22). Key-free.

Three failures from the live call, each with its exact repro:
  1. A 'manual'-routed card ("Runs through: Nobody") whose approve-time retype
     made it executable was CLAIMED with no Google connection → a doomed
     "Couldn't complete". Now: honest tracked-only + reconnect hint.
  2. "…please? Ducho, do you wanna discuss something else?" — a new turn
     aimed at another participant was GLUED onto the captured card (both the
     clarify-details path and the ASR-continuation path could do it).
  3. The humans hung up and the bot sat in the empty room on the paid meter
     until a manual End — nothing watched the roster.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time as _time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402


# ── shared webhook harness (same shape as test_live_followup_and_corrections) ──


def _session(tmp_path, monkeypatch, bot_id="round2-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/rnd-two-tst", "laura")
    s.addressed_once = True
    return s


def _line(bot_id: str, speaker: str, pid, text: str) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": w} for w in text.split()],
                "participant": {"name": speaker, "id": pid},
            },
        },
    }


async def _post_async(payload: dict) -> dict:
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    resp = await main.recall_webhook(FakeRequest())
    return json.loads(resp.body)


def _post(payload: dict) -> dict:
    return asyncio.run(_post_async(payload))


def _mute(monkeypatch) -> list:
    spoken: list = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    return spoken


# ── 2. capture glue: a turn aimed at someone else never joins the card ──


def _capture_recap(s, monkeypatch) -> list:
    _mute(monkeypatch)
    _post(_line(s.bot_id, "Kai", 1, "Laura, send the recap email to Anant"))
    queued = getattr(s, "queued_actions", None) or []
    assert len(queued) == 1, "setup: expected one captured card"
    return queued


def test_roster_vocative_not_glued_onto_card(tmp_path, monkeypatch):
    """Same speaker, seconds later, turns to a ROSTER participant."""
    s = _session(tmp_path, monkeypatch, bot_id="glue-roster-1")
    s.memory_brief = ""
    s.participant_event("Kai", 1, here=True)
    s.participant_event("Marco", 2, here=True)
    queued = _capture_recap(s, monkeypatch)
    baseline = queued[0].get("action") or ""

    _post(_line(s.bot_id, "Kai", 1, "Marco, do you wanna discuss something else?"))
    assert (queued[0].get("action") or "") == baseline, (
        "a turn addressed to another participant must never be glued"
    )
    store.remove(s.bot_id)


def test_mangled_leading_vocative_not_glued(tmp_path, monkeypatch):
    """The live case: 'Ducho' (ASR-mangled, NOT in the roster) — the leading
    'Name, …' net must catch what the roster fuzzy match misses."""
    s = _session(tmp_path, monkeypatch, bot_id="glue-mangled-1")
    s.memory_brief = ""
    s.participant_event("Kai", 1, here=True)
    s.participant_event("Duccio", 2, here=True)
    queued = _capture_recap(s, monkeypatch)
    baseline = queued[0].get("action") or ""

    _post(_line(s.bot_id, "Kai", 1, "Ducho, do you wanna discuss something else?"))
    assert (queued[0].get("action") or "") == baseline
    assert "discuss" not in (queued[0].get("action") or "")
    store.remove(s.bot_id)


def test_real_detail_answer_still_extends(tmp_path, monkeypatch):
    """Guard: a genuine detail answer (no vocative) still resolves clarify."""
    s = _session(tmp_path, monkeypatch, bot_id="glue-guard-1")
    s.memory_brief = ""
    s.participant_event("Kai", 1, here=True)
    s.participant_event("Marco", 2, here=True)
    queued = _capture_recap(s, monkeypatch)

    if getattr(s, "pending_clarify", None) is not None:
        _post(_line(s.bot_id, "Kai", 1, "due Friday, saying the notes are ready"))
        assert "Friday" in (queued[0].get("action") or ""), queued
    store.remove(s.bot_id)


# ── 3. empty room: the bot leaves on its own (meter safety) ──


def test_empty_room_auto_finalizes(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="empty-room-1")
    s.participant_event("Kai", 1, here=True)
    s.participant_event("Kai", 1, here=False)  # last human gone
    assert s.roster("Laura") == []

    monkeypatch.setattr(main, "_EMPTY_ROOM_GRACE_S", 0.05)
    finalized = {}

    async def fake_finalize(bot_id, source="", **kw):
        finalized["bot"] = bot_id
        finalized["source"] = source
        return {}

    monkeypatch.setattr(main, "_finalize_session", fake_finalize)

    async def scenario():
        main._schedule_empty_room_leave(s)
        assert getattr(s, "empty_room_task", None) is not None
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert finalized.get("source") == "empty_room"
    assert finalized.get("bot") == s.bot_id
    store.remove(s.bot_id)


def test_empty_room_rejoin_cancels_finalize(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="empty-room-2")
    s.participant_event("Kai", 1, here=True)
    s.participant_event("Kai", 1, here=False)

    monkeypatch.setattr(main, "_EMPTY_ROOM_GRACE_S", 0.05)
    finalized = {}

    async def fake_finalize(bot_id, source="", **kw):
        finalized["source"] = source
        return {}

    monkeypatch.setattr(main, "_finalize_session", fake_finalize)

    async def scenario():
        main._schedule_empty_room_leave(s)
        s.participant_event("Kai", 1, here=True)  # rejoined during grace
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert not finalized, "a rejoin during the grace window must cancel the leave"
    store.remove(s.bot_id)


def test_leave_rejoin_leave_gets_a_fresh_full_grace(tmp_path, monkeypatch):
    """The second leave must not inherit the first leave's older deadline."""
    s = _session(tmp_path, monkeypatch, bot_id="empty-room-renew")
    s.participant_event("Kai", 1, here=True)

    monkeypatch.setattr(main, "_EMPTY_ROOM_GRACE_S", 0.08)
    finalized = []

    async def fake_finalize(bot_id, source="", **kw):
        finalized.append((bot_id, source))
        return {}

    monkeypatch.setattr(main, "_finalize_session", fake_finalize)

    async def scenario():
        s.participant_event("Kai", 1, here=False)
        main._schedule_empty_room_leave(s)
        await asyncio.sleep(0.03)

        s.participant_event("Kai", 1, here=True)
        main._invalidate_empty_room_leave(s)
        s.participant_event("Kai", 1, here=False)
        main._schedule_empty_room_leave(s)

        # The original deadline has passed, but the renewed one has not.
        await asyncio.sleep(0.06)
        assert not finalized

        await asyncio.sleep(0.04)

    asyncio.run(scenario())
    assert finalized == [(s.bot_id, "empty_room")]
    store.remove(s.bot_id)


def test_leave_webhook_schedules_empty_room_check(tmp_path, monkeypatch):
    """End-to-end through the webhook: the LAST leave event arms the check."""
    s = _session(tmp_path, monkeypatch, bot_id="empty-room-3")
    _mute(monkeypatch)

    def leave_payload(pid, name):
        return {
            "event": "participant_events.leave",
            "data": {
                "bot": {"id": s.bot_id},
                "data": {"participant": {"id": pid, "name": name}},
            },
        }

    async def scenario():
        join = dict(leave_payload(1, "Kai"))
        join["event"] = "participant_events.join"
        await _post_async(join)
        assert getattr(s, "empty_room_task", None) is None

        await _post_async(leave_payload(1, "Kai"))
        assert getattr(s, "empty_room_task", None) is not None
        main._invalidate_empty_room_leave(s)  # deterministic test cleanup

    asyncio.run(scenario())
    store.remove(s.bot_id)


def test_agent_join_during_grace_keeps_meter_finalize_armed(tmp_path, monkeypatch):
    """A bot/agent join must NOT cancel a pending empty-room finalize.

    Empty-room grace is a HUMAN-presence property (roster() counts only humans).
    A join never reschedules, so if an agent/bot join (bot reconnect, co-avatar)
    were allowed to cancel the pending finalize, the room would sit human-empty
    with NO timer and the per-minute meter would leak until the call actually
    ends. The empty-room block is therefore gated on kind != agent.
    """
    s = _session(tmp_path, monkeypatch, bot_id="empty-room-agent-join")
    _mute(monkeypatch)

    def participant_payload(event, pid, name, extra=None):
        p = {"id": pid, "name": name}
        if extra:
            p.update(extra)
        return {
            "event": event,
            "data": {"bot": {"id": s.bot_id}, "data": {"participant": p}},
        }

    async def scenario():
        # A human joins then leaves → the last-human leave arms the finalize.
        await _post_async(participant_payload("participant_events.join", 1, "Kai"))
        await _post_async(participant_payload("participant_events.leave", 1, "Kai"))
        armed = getattr(s, "empty_room_task", None)
        assert armed is not None and not armed.done()

        # A bot/agent join lands during the grace window (is_agent → kind=agent).
        await _post_async(
            participant_payload(
                "participant_events.join", 99, "Laura", {"is_agent": True}
            )
        )

        # Meter-safety invariant: the pending finalize survives, untouched.
        assert getattr(s, "empty_room_task", None) is armed
        assert not armed.cancelled()
        main._invalidate_empty_room_leave(s)  # deterministic cleanup

    asyncio.run(scenario())
    store.remove(s.bot_id)


# ── 1. manual-routed card + no Google → tracked-only, never a doomed claim ──


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAURA_STORE_PATH", str(tmp_path / "store.sqlite3"))
    from app import ledger as ledger_mod

    importlib.reload(store)
    importlib.reload(ledger_mod)
    monkeypatch.setattr(settings, "native_executor", True)
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def _login(client):
    from app import auth

    user = store.upsert_user("owner@x.com")
    client.cookies.set(auth.COOKIE_NAME, auth.make_cookie(user["user_id"]))
    return user


def test_manual_route_without_google_is_tracked_only(app_client, monkeypatch):
    """The exact live failure: route='manual' ("Runs through: Nobody"), the
    approve-time retype makes it executable, org has NO Google connection.
    Before: claim → native execute → 'Couldn't complete'. Now: approved ·
    tracked only + reconnect hint, vendor never called."""
    from app import executor, ledger

    user = _login(app_client)
    action = {
        "item": "Schedule the meeting with that email",
        "owner": "Kai",
        "action_id": "r2a1",
        "execution_route": "manual",
        "typed": {
            "type": "calendar.create_event",
            "args": {"title": "Sync", "start": "2026-07-24T15:00:00",
                     "end": "2026-07-24T15:30:00",
                     "attendees": ["anant@example.com"]},
        },
    }
    store.save_artifact(
        "bot_r2a1",
        {
            "summary": "Test.",
            "actions": [action],
            "checklist": [action],
            "org_id": user["org_id"],
            "avatar_id": "laura",
            "meeting_url": "https://meet.google.com/r2a-test",
            "transcript": "PII must never leak",
        },
        org_id=user["org_id"],
    )
    calls: list = []
    monkeypatch.setattr(
        executor.google_client, "create_calendar_event",
        lambda org, event: calls.append((org, event)) or {"ok": True},
    )

    r = app_client.post("/dashboard/actions/r2a1/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["executed"] is False, body
    assert body["connection_blocked"] is True, body
    assert not calls, "the vendor must never be called without a connection"
    st = ledger.action_statuses(["r2a1"], org_id=user["org_id"]).get("r2a1")
    assert st and st["status"] == "approved", st
    assert "Google isn't connected" in st["detail"], st
