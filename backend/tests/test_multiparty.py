"""Multi-party awareness: live roster + who-is-addressed turn-taking. No keys."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.decision import addressed_to_other  # noqa: E402


# ── roster: event-driven, with transcript fallback ──


def _session(tmp_path, monkeypatch, bot_id="roster-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    # These scenarios model a meeting already UNDERWAY (roster built, turns
    # taken). Push past the opening settle-in grace so unaddressed lines exercise
    # deference / greeting / nudge / follow-up instead of being held silent — the
    # grace itself is covered on its own in test_opening_grace.py.
    s.created_at -= settings.opening_grace_seconds + 1
    return s


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


# ── round 3: engaged follow-up, quiet-awareness, fuzzy exclusion ──

import time as _time  # noqa: E402


def test_followup_question_bypasses_cooldown_and_deference(tmp_path, monkeypatch):
    """She just answered; a nameless follow-up question must be answered
    immediately — no cooldown block, no deference wait."""
    from app.config import settings

    s = _session(tmp_path, monkeypatch, bot_id="followup-bot-1")
    s.memory_brief = ""
    s.add_utterance("Duccio", "Laura what's the next onboarding step?")
    s.mark_spoke()  # she just answered (inside cooldown AND followup window)
    monkeypatch.setattr(settings, "deference_seconds", 30.0)  # would hang if hit

    def instant_answer(*a, **k):
        yield "The DPA comes right after security review."

    monkeypatch.setattr(main, "answer_question_stream", instant_answer)
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    body = _post(_line_payload(s.bot_id, "Duccio", "and what about the deadline?"))
    assert body.get("reason") not in ("cooldown", "deferred to human"), body
    assert spoken, "follow-up question must be answered"
    store.remove(s.bot_id)


def test_followup_statement_still_respects_cooldown(tmp_path, monkeypatch):
    """Only QUESTIONS ride the follow-up window; a statement right after her
    answer stays throttled by the cooldown."""
    s = _session(tmp_path, monkeypatch, bot_id="followup-bot-2")
    s.memory_brief = ""
    s.mark_spoke()
    body = _post(_line_payload(s.bot_id, "Duccio", "ok that makes sense to me"))
    assert body.get("reason") == "cooldown"
    store.remove(s.bot_id)


def test_old_answer_does_not_open_followup_window(tmp_path, monkeypatch):
    """A question long after she spoke is a room question again: deference on."""
    from app.config import settings

    s = _session(tmp_path, monkeypatch, bot_id="followup-bot-3")
    s.memory_brief = ""
    s.last_spoke_at = _time.time() - 60  # spoke a minute ago; window is 15s
    monkeypatch.setattr(settings, "deference_seconds", 0.05)

    real_sleep = asyncio.sleep

    async def sleep_and_interject(seconds):
        s.add_utterance("Marco", "I think it's Friday")
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", sleep_and_interject)
    body = _post(_line_payload(s.bot_id, "Duccio", "when is the deadline?"))
    assert body.get("reason") == "deferred to human"
    store.remove(s.bot_id)


def test_fuzzy_wake_excluded_for_real_participant_named_lara(tmp_path, monkeypatch):
    avatar = avatars.load("laura")
    # Without a Lara in the room, "Lara" is an ASR corruption -> wakes.
    called, _ = detect_wake(avatar, "Lara, what's the next step?")
    assert called
    # With a real Lara present, it's HER being addressed -> silent.
    called, _ = detect_wake(
        avatar, "Lara, what's the next step?", exclude_names=["Lara Bianchi"]
    )
    assert not called
    # The exact wake word still always wins, even with a Lara present.
    called, _ = detect_wake(
        avatar, "Laura, what's the next step?", exclude_names=["Lara Bianchi"]
    )
    assert called


def test_roster_block_lists_quiet_participants():
    from app.brain import _roster_block
    from app.meeting_state import MeetingState, update

    avatar = avatars.load("laura")
    state = MeetingState()
    update(state, "Duccio", "let's review the onboarding", wake_words=["laura"])
    block = _roster_block(avatar, ["Duccio", "Marco Rossi", "Anna"], state)
    assert "3 people" in block
    assert "Not yet heard from" in block
    assert "Marco Rossi" in block and "Anna" in block
    assert "Duccio" in block.split("Not yet heard from")[0]
    # everyone spoke -> no quiet line
    update(state, "Marco Rossi", "sounds good", wake_words=["laura"])
    update(state, "Anna", "agreed", wake_words=["laura"])
    block = _roster_block(avatar, ["Duccio", "Marco Rossi", "Anna"], state)
    assert "Not yet heard from" not in block


# ── echo guard + ack discipline (live-test fixes) ──


def _partial_line_payload(bot_id: str, speaker: str, text: str) -> dict:
    p = _line_payload(bot_id, speaker, text)
    p["event"] = "transcript.partial_data"
    return p


def test_echo_final_line_not_answered_not_stored(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="echo-bot-1")
    s.memory_brief = ""
    # she spoke this 2s ago (recorded by the repetition guard)
    spoken_line = "The DPA confirmation comes right after the security review."
    s._recent_lines[main._norm_line(spoken_line)] = _time.time() - 2
    before = len(s.transcript)
    body = _post(_line_payload(s.bot_id, "Duccio", spoken_line))
    assert body.get("reason") == "echo"
    assert len(s.transcript) == before, "echo must not pollute the transcript"
    store.remove(s.bot_id)


def test_echo_partial_does_not_barge_or_stamp(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="echo-bot-2")
    spoken_line = "The DPA confirmation comes right after the security review."
    s._recent_lines[main._norm_line(spoken_line)] = _time.time() - 1
    s.speaking_until = _time.time() + 5  # she's mid-answer
    stopped = []

    async def fake_stop(session):
        stopped.append(True)

    monkeypatch.setattr(main, "_make_avatar_stop", fake_stop)
    # echo partial: a chunk of her own sentence, attributed to a human
    body = _post(_partial_line_payload(s.bot_id, "Duccio", "The DPA confirmation comes right after"))
    assert body.get("echo") is True
    assert not stopped, "her own echo must never barge in on her"
    assert s.last_human_partial_at == 0.0, "echo must not cancel deference"
    store.remove(s.bot_id)


def test_filler_partial_does_not_barge_in(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="filler-bot-1")
    s.speaking_until = _time.time() + 5
    assert not main._should_barge_in(s, "Laura", "Duccio", "yeah yeah okay")
    assert not main._should_barge_in(s, "Laura", "Duccio", "sì sì va bene")
    assert main._should_barge_in(s, "Laura", "Duccio", "wait I have a question")
    store.remove(s.bot_id)


def test_partial_ack_requires_exact_name_and_a_forming_question(tmp_path, monkeypatch):
    from app.config import settings

    s = _session(tmp_path, monkeypatch, bot_id="ack-bot-1")
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    monkeypatch.setattr(settings, "backchannel_enabled", False)

    # bare name: no ack yet (the question hasn't formed)
    _post(_partial_line_payload(s.bot_id, "Duccio", "Laura"))
    assert not spoken
    # fuzzy/corrupted name on a partial: never an audible ack
    _post(_partial_line_payload(s.bot_id, "Duccio", "Lara what's the process"))
    assert not spoken
    # exact name + question forming: ack fires
    _post(_partial_line_payload(s.bot_id, "Duccio", "Laura what's the process"))
    assert spoken, "exact name with 3+ words should ack"
    store.remove(s.bot_id)
