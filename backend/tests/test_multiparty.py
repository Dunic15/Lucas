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


# ── fuzzy name matching (ASR-corrupted names) ──

from app import avatars  # noqa: E402
from app.decision import detect_wake, fuzzy_name_match  # noqa: E402


def test_fuzzy_wake_on_asr_corrupted_name():
    avatar = avatars.load("laura")
    for utterance in [
        "Lara, what's the next step?",
        "hey Lora can you help us",
        "Loura what do you think?",
    ]:
        called, _ = detect_wake(avatar, utterance)
        assert called, f"corrupted name should wake: {utterance!r}"


def test_fuzzy_wake_does_not_fire_on_lookalike_words():
    avatar = avatars.load("laura")
    for utterance in [
        "ho preso la laurea l'anno scorso",  # IT: degree — dist 1, excluded
        "Clara said the deadline moved",     # different first letter
        "loro hanno già firmato il contratto",  # IT: "they" — too far
        "we discussed the launch timeline",
        "a che ora è la riunione di domani",  # "l'ora" must not tokenize into lora
    ]:
        called, _ = detect_wake(avatar, utterance)
        assert not called, f"must NOT wake: {utterance!r}"


def test_fuzzy_wake_reported_speech_still_suppressed():
    avatar = avatars.load("laura")
    called, _ = detect_wake(avatar, "as Lara said earlier, we should ship")
    assert not called, "reported speech with corrupted name must not wake"


def test_addressed_to_other_fuzzy_name():
    assert addressed_to_other("Marko, can you take this?", ROSTER)
    assert addressed_to_other("what's your view on this, Marcko?", ROSTER)


def test_fuzzy_name_match_unit():
    assert fuzzy_name_match("lara", "laura")
    assert fuzzy_name_match("lora", "laura")  # dist 2, same consonant skeleton
    assert not fuzzy_name_match("libra", "laura")  # dist 2, skeleton differs
    assert not fuzzy_name_match("clara", "laura")
    assert not fuzzy_name_match("laurea", "laura")  # excluded dictionary word


# ── deference window: humans get first right of reply ──


def test_deference_yields_when_a_human_answers(tmp_path, monkeypatch):
    from app.config import settings

    s = _session(tmp_path, monkeypatch, bot_id="defer-bot-1")
    s.memory_brief = ""
    monkeypatch.setattr(settings, "deference_seconds", 0.05)
    # seed enough transcript that the line isn't the meeting opener
    s.add_utterance("Duccio", "let's get started")

    real_sleep = asyncio.sleep

    async def sleep_and_interject(seconds):
        # a human starts answering while she politely waits
        s.add_utterance("Marco", "I can take that one")
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", sleep_and_interject)
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    body = _post(_line_payload(s.bot_id, "Duccio", "who can take the rollout?"))
    assert body.get("reason") == "deferred to human"
    assert not spoken
    store.remove(s.bot_id)


def test_no_deference_when_called_by_name(tmp_path, monkeypatch):
    from app.config import settings

    s = _session(tmp_path, monkeypatch, bot_id="defer-bot-2")
    s.memory_brief = ""
    monkeypatch.setattr(settings, "deference_seconds", 30.0)  # would hang if hit

    def instant_answer(*a, **k):
        yield "The next step is the security review."

    monkeypatch.setattr(main, "answer_question_stream", instant_answer)

    async def fake_speak(session, line, citations=None, **kw):
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    body = _post(_line_payload(s.bot_id, "Duccio", "Laura, what's the next step?"))
    assert body.get("reason") != "deferred to human"
    store.remove(s.bot_id)


def _line_payload(bot_id: str, speaker: str, text: str) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": w} for w in text.split()],
                "participant": {"name": speaker, "id": 1},
            },
        },
    }


# ── footing: greet late joiners, nudge quiet participants ──


def test_greets_new_joiner_mid_meeting(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="greet-bot-1")
    for i in range(4):
        s.add_utterance("Duccio", f"point number {i} about the onboarding")
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    _post(_participant_payload(s.bot_id, "participant_events.join", "Anna", 7))
    assert spoken and "Anna" in spoken[0]
    # the same join event again must not greet twice
    spoken.clear()
    _post(_participant_payload(s.bot_id, "participant_events.join", "Anna", 7))
    assert not spoken
    store.remove(s.bot_id)


def test_no_greeting_at_meeting_start(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="greet-bot-2")
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    _post(_participant_payload(s.bot_id, "participant_events.join", "Anna", 7))
    assert not spoken  # transcript empty: everyone is greeting anyway
    store.remove(s.bot_id)


def test_quiet_participant_nudged_at_wrapup(tmp_path, monkeypatch):
    from app.config import settings

    s = _session(tmp_path, monkeypatch, bot_id="nudge-bot-1")
    s.memory_brief = ""
    # isolate the nudge: the proactive gap intervention wins the wrap-up slot
    monkeypatch.setattr(settings, "proactive_enabled", False)
    s.participant_event("Anna", 3, here=True)  # in the room, never spoke
    for i in range(12):
        s.add_utterance("Duccio" if i % 2 else "Marco", f"working point {i}")
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    body = _post(_line_payload(s.bot_id, "Duccio", "okay, anything else before we wrap up?"))
    assert body.get("quiet_nudge") is True
    assert spoken and "Anna" in spoken[0]
    # one-shot: a second closing cue must not nudge again
    spoken.clear()
    body = _post(_line_payload(s.bot_id, "Duccio", "alright, let's wrap up then"))
    assert body.get("quiet_nudge") is None
    store.remove(s.bot_id)
