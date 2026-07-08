"""Multi-party awareness: live roster + who-is-addressed turn-taking. No keys."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.decision import addressed_to_other  # noqa: E402


# ── roster: event-driven, with transcript fallback ──


def _session(tmp_path, monkeypatch, bot_id="roster-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    return store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")


def test_roster_tracks_joins_and_leaves(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.participant_event("Duccio", 1, here=True)
    s.participant_event("Marco", 2, here=True)
    s.participant_event("Anna", 3, here=True)
    assert s.roster("Laura") == ["Duccio", "Marco", "Anna"]
    s.participant_event("Anna", 3, here=False)  # Anna drops
    assert s.roster("Laura") == ["Duccio", "Marco"]
    store.remove(s.bot_id)


def test_roster_excludes_the_avatar_itself(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.participant_event("Laura", 99, here=True)
    s.participant_event("Duccio", 1, here=True)
    assert s.roster("Laura") == ["Duccio"]
    store.remove(s.bot_id)


def test_roster_falls_back_to_transcript_speakers(tmp_path, monkeypatch):
    """After a restart the event roster is empty — people who spoke still count."""
    s = _session(tmp_path, monkeypatch)
    s.add_utterance("Duccio", "let's get started")
    s.add_utterance("Marco", "sounds good")
    s.add_utterance("Laura", "happy to help")  # her own lines never count
    assert s.roster("Laura") == ["Duccio", "Marco"]
    store.remove(s.bot_id)


def test_roster_left_participant_not_resurrected_by_transcript(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.participant_event("Anna", 3, here=True)
    s.add_utterance("Anna", "I have to drop, bye")
    s.participant_event("Anna", 3, here=False)
    assert s.roster("Laura") == []
    store.remove(s.bot_id)


def test_anonymous_participants_get_distinct_guest_labels(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.participant_event(None, "p-1", here=True)
    s.participant_event(None, "p-2", here=True)
    assert s.roster("Laura") == ["Guest 1", "Guest 2"]
    store.remove(s.bot_id)


# ── webhook: participant_events reach the session roster ──


def _participant_payload(bot_id: str, event: str, name: str, pid) -> dict:
    return {
        "event": event,
        "data": {
            "bot": {"id": bot_id},
            "data": {"participant": {"id": pid, "name": name}},
        },
    }


def _post(payload: dict) -> dict:
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    resp = asyncio.run(main.recall_webhook(FakeRequest()))
    return json.loads(resp.body)


def test_webhook_participant_events_update_roster(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="roster-webhook-bot")
    _post(_participant_payload(s.bot_id, "participant_events.join", "Duccio", 1))
    _post(_participant_payload(s.bot_id, "participant_events.join", "Marco", 2))
    _post(_participant_payload(s.bot_id, "participant_events.join", "Laura", 9))
    assert s.roster("Laura") == ["Duccio", "Marco"]
    _post(_participant_payload(s.bot_id, "participant_events.leave", "Marco", 2))
    assert s.roster("Laura") == ["Duccio"]
    store.remove(s.bot_id)


def test_webhook_participant_event_without_session_is_noop():
    body = _post(_participant_payload("no-such-bot", "participant_events.join", "X", 1))
    assert body.get("ok") is True


# ── turn-taking: a line aimed at another participant by name is not hers ──

ROSTER = ["Duccio", "Marco Rossi", "Guest 1"]

ADDRESSED_TO_OTHER = [
    "Marco, can you take this?",
    "marco, what do you think",
    "hey Marco what's your read on this",
    "ok Marco, over to you",
    "so that's the plan. Marco, your turn",
    "what do you think, Marco?",
    "Duccio, puoi condividere lo schermo?",
    "senti Marco, andiamo avanti noi",
]

NOT_ADDRESSED_TO_OTHER = [
    # a mention mid-sentence is normal meeting talk, not a hand-off
    "Marco will own the rollout",
    "I agree with Marco on the timeline",
    "did Marco send the doc?",
    "what's the next step for onboarding?",
    "how many people are in this meeting?",
    "Guest 1 had a question earlier",  # anonymous labels aren't spoken vocatives
    "",
]


def test_addressed_to_other_detected():
    for line in ADDRESSED_TO_OTHER:
        assert addressed_to_other(line, ROSTER), f"should detect: {line!r}"


def test_not_addressed_to_other():
    for line in NOT_ADDRESSED_TO_OTHER:
        assert not addressed_to_other(line, ROSTER), f"must NOT detect: {line!r}"


def test_addressed_to_other_ignores_unknown_names():
    assert not addressed_to_other("Giulia, can you take this?", ROSTER)
