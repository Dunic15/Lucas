"""Multi-party awareness: live roster + who-is-addressed turn-taking. No keys."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.decision import addressed_to_other, adaptive_deference_seconds  # noqa: E402


# ── roster: event-driven, with transcript fallback ──


def _session(tmp_path, monkeypatch, bot_id="roster-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    # These scenarios model a meeting already UNDERWAY (roster built, turns
    # taken, avatar already named once). Mark her activated so unaddressed lines
    # exercise deference / greeting / nudge / follow-up instead of being held
    # silent — the first-call gate is covered on its own in test_opening_grace.py.
    s.addressed_once = True
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


def test_roster_excludes_explicit_agent_not_same_name_human(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    s.participant_event(
        "Laura", 99, here=True, metadata={"id": 99, "is_bot": True}
    )
    s.participant_event("Laura", 100, here=True, metadata={"id": 100})
    s.participant_event("Duccio", 1, here=True)
    assert s.roster("Laura") == ["Laura", "Duccio"]
    store.remove(s.bot_id)


def test_roster_falls_back_to_transcript_speakers(tmp_path, monkeypatch):
    """After a restart the event roster is empty — people who spoke still count."""
    s = _session(tmp_path, monkeypatch)
    s.add_utterance("Duccio", "let's get started")
    s.add_utterance("Marco", "sounds good")
    s.add_utterance(
        "Laura",
        "happy to help",
        participant_id="bot-participant",
        speaker_kind="agent",
    )
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


def _participant_payload(
    bot_id: str, event: str, name: str, pid, **participant_metadata
) -> dict:
    return {
        "event": event,
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "participant": {
                    "id": pid,
                    "name": name,
                    **participant_metadata,
                }
            },
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
    _post(
        _participant_payload(
            s.bot_id, "participant_events.join", "Laura", 9, is_bot=True
        )
    )
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
    "Marco can you confirm the total",
    "Marko could you share your screen",
    "marco, what do you think",
    "Marco what do you think",
    "hey Marco what's your read on this",
    "ok Marco, over to you",
    "so that's the plan. Marco, your turn",
    "what do you think, Marco?",
    "Duccio, puoi condividere lo schermo?",
    "Duccio puoi condividere lo schermo",
    "Duccio cosa ne pensi",
    "senti Marco, andiamo avanti noi",
]

NOT_ADDRESSED_TO_OTHER = [
    # a mention mid-sentence is normal meeting talk, not a hand-off
    "Marco will own the rollout",
    "Marco can confirm the total",
    "Marco could own the rollout",
    "Marco che lavora con noi presenterà il piano",
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


def test_fuzzy_wake_does_not_fire_on_italian_words_for_the_live_avatar():
    """The avatar actually in production ships the aliases "lara"/"lora" for
    ASR corruption of "Laura". Those are FOUR letters, so edit-distance-1 pulls
    in ordinary Italian vocabulary — "loro" ("they") is one substitution from
    "lora" and turns up several times a minute in an Italian call.

    A false wake here is not a harmless extra answer: it opens the multiparty
    Director gate and streams a private human-to-human exchange straight into
    ElevenLabs. The `laura`-avatar test above misses this entirely — its only
    wake word is the full "laura", which "loro" is three edits away from."""
    avatar = avatars.load("petra")
    assert "lora" in avatar.wake_words, "guard: the collision-prone alias is live"
    for utterance in [
        "loro hanno già firmato il contratto",
        "secondo loro la deadline è venerdì",
        "the lord of the rings marathon is on saturday",
        "la lana e la lava sono materiali diversi",
        "questa finestra è troppo larga per lo schermo",
    ]:
        called, _ = detect_wake(avatar, utterance)
        assert not called, f"must NOT wake: {utterance!r}"


def test_genuine_asr_corruptions_still_wake_the_live_avatar():
    """The exclusions must not cost her the corruptions they exist for."""
    avatar = avatars.load("petra")
    for utterance in [
        "Lara, what's the next step?",
        "hey Lora can you help us",
        "Loura what do you think?",
        "Laura, quando è la deadline?",
    ]:
        called, _ = detect_wake(avatar, utterance)
        assert called, f"corrupted name should wake: {utterance!r}"


def test_question_to_her_survives_a_dropped_vocative_comma():
    """Prod transcribes with Deepgram in prioritize_low_latency, which routinely
    drops the comma in "Laura, was that in the contract?". Bare "laura was" then
    looked like reported speech ("Laura was right") and she stayed silent
    through a question aimed straight at her — in multiparty that also means the
    Director gate never opened, so the ask never even reached the model."""
    avatar = avatars.load("petra")
    for utterance in [
        "Laura was that in the contract",
        "Laura were we supposed to ship friday",
        "Laura had we agreed on this already",
        "Laura was it three or four",
        "Laura was there a decision on hiring",
    ]:
        called, _ = detect_wake(avatar, utterance)
        assert called, f"a question to her must wake: {utterance!r}"


def test_talking_about_her_is_still_not_addressing_her():
    """The inversion carve-out must not swallow genuine third-person talk."""
    avatar = avatars.load("petra")
    for utterance in [
        "Laura was right about the risk",
        "Laura had a good point there",
        "Laura was the one who flagged it",
        "Laura mentioned the deadline",
        "what did Laura mean by handoff",
    ]:
        called, _ = detect_wake(avatar, utterance)
        assert not called, f"talk ABOUT her must not wake: {utterance!r}"


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


# ── adaptive deference: sizes ONLY the wait, never the yield decision ──
# Pure-function tests (no Session): the helper takes floats/int/bool and returns
# a wait length. The single load-bearing invariant is that with adaptation OFF
# (or a mis-configured range) it returns `base` verbatim, so the existing
# webhook deference tests below — which monkeypatch a tiny base — are unaffected.

_DEFER_KW = dict(
    lo=1.0, hi=2.6, since_partial=5.0, active_partial_seconds=0.6,
    n_humans=3, is_question=False,
)


def test_adaptive_deference_disabled_is_strict_noop():
    # OFF → base verbatim, regardless of what the range would otherwise pick.
    assert adaptive_deference_seconds(1.8, enabled=False, **_DEFER_KW) == 1.8
    # A monkeypatched tiny base (as the webhook tests use) is returned untouched.
    assert adaptive_deference_seconds(0.05, enabled=False, **_DEFER_KW) == 0.05


def test_adaptive_deference_bad_range_falls_back_to_base():
    # lo >= hi, a negative bound, OR lo == 0 must NOT collapse the yield window.
    # lo == 0 matters specifically: the single-human branch returns `lo`, so a
    # zero min would sleep(0) and give a human no chance to take the floor.
    assert adaptive_deference_seconds(
        1.8, enabled=True, **{**_DEFER_KW, "lo": 2.6, "hi": 2.6}
    ) == 1.8
    assert adaptive_deference_seconds(
        1.8, enabled=True, **{**_DEFER_KW, "lo": -1.0, "hi": 2.6}
    ) == 1.8
    assert adaptive_deference_seconds(
        1.8, enabled=True, **{**_DEFER_KW, "lo": 0.0, "n_humans": 1}
    ) == 1.8


def test_adaptive_deference_extends_on_mid_utterance_partial():
    # A human partial landed within the active window → wait the max (give room).
    assert adaptive_deference_seconds(
        1.8, enabled=True, **{**_DEFER_KW, "since_partial": 0.2}
    ) == 2.6


def test_adaptive_deference_shortens_for_single_human():
    # Only one human present → nobody to defer to → respond snappily (min).
    assert adaptive_deference_seconds(
        1.8, enabled=True, **{**_DEFER_KW, "n_humans": 1}
    ) == 1.0


def test_adaptive_deference_splits_for_room_open_question():
    # Several humans + a question → between base and min.
    assert adaptive_deference_seconds(
        1.8, enabled=True, **{**_DEFER_KW, "is_question": True}
    ) == (1.8 + 1.0) / 2.0


def test_adaptive_deference_statement_uses_base():
    # Several humans + a statement (stale partial) → the base wait.
    assert adaptive_deference_seconds(1.8, enabled=True, **_DEFER_KW) == 1.8


def test_adaptive_deference_always_within_bounds():
    # Whatever the inputs, the result never escapes [lo, hi].
    for since in (0.0, 0.5, 5.0):
        for n in (1, 2, 5):
            for q in (True, False):
                w = adaptive_deference_seconds(
                    1.8, enabled=True, lo=1.0, hi=2.6,
                    since_partial=since, active_partial_seconds=0.6,
                    n_humans=n, is_question=q,
                )
                assert 1.0 <= w <= 2.6, (since, n, q, w)


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
    from app.brain.engine import _roster_block
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


# ── fuzzy-wake exclude set: shield transcript-only participants ──


def test_present_names_shields_transcript_only_participant(tmp_path, monkeypatch):
    """A real "Lara" known ONLY from the transcript (she spoke but never fired a
    join event) must be shielded from the fuzzy wake. present_names() now merges
    transcript speakers (backed by roster()), so "Lara, …" is her turn — not a
    corruption of "Laura". Strictly reduces false wakes; never makes her speak
    more."""
    from app.avatars import Avatar
    from app.decision import detect_wake

    s = _session(tmp_path, monkeypatch, bot_id="shield-bot")
    s.add_utterance("Lara", "can you pull the latest numbers?")  # no join event
    avatar = Avatar(
        id="laura", name="Laura", role="x", wake_words=["laura"], persona_prompt="",
        anam_avatar_id="r", elevenlabs_voice_id="v", min_confidence=0.55,
        speak_cooldown_seconds=8.0, dir=Path("."),
    )
    line = "Lara, can you pull the latest numbers?"
    # Baseline: with no exclude set, "Lara" fuzzy-wakes "Laura".
    assert detect_wake(avatar, line, [])[0] is True
    # The transcript-only participant is now in the exclude set …
    assert "lara" in {n.lower() for n in s.present_names()}
    # … so the fuzzy wake is suppressed: it's Lara's turn, not Laura's.
    assert detect_wake(avatar, line, s.present_names())[0] is False
    store.remove(s.bot_id)


# ── closing fallback: facilitation beats also fire on a natural lull ──
# (DEMO-READY-ROADMAP §5 item 12). The proactive wrap-up + quiet-participant
# nudge no longer depend on the exact detect_closing() phrase: a long idle gap
# after a long-enough meeting is an additive trigger. Conservative — both a
# duration gate and an idle gate must hold, so it never fires in a short or
# actively-talking call.


def _seed_wrapup_session(tmp_path, monkeypatch, bot_id, *, meeting_age, idle):
    from app.config import settings

    s = _session(tmp_path, monkeypatch, bot_id=bot_id)
    s.memory_brief = ""
    monkeypatch.setattr(settings, "proactive_enabled", False)  # isolate the nudge
    monkeypatch.setattr(settings, "deference_seconds", 0)  # deterministic, no wait
    s.participant_event("Anna", 3, here=True)  # in the room, never spoke
    for i in range(12):
        s.add_utterance("Duccio" if i % 2 else "Marco", f"working point {i}")
    now = _time.time()
    s.transcript[-1].ts = now - idle       # the room went quiet `idle`s ago
    s.created_at = now - meeting_age       # meeting has run `meeting_age`s
    return s


def test_closing_fallback_nudges_on_idle_lull_without_phrase(tmp_path, monkeypatch):
    """No exact closing phrase, but a 30s lull after a 5-min meeting → the quiet
    nudge fires via the fallback."""
    s = _seed_wrapup_session(
        tmp_path, monkeypatch, "nudge-fallback-1", meeting_age=300, idle=30
    )
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    # A plain line that detect_closing() does NOT match.
    body = _post(_line_payload(s.bot_id, "Duccio", "so, where does that leave everyone?"))
    assert body.get("quiet_nudge") is True
    assert spoken and "Anna" in spoken[0]
    store.remove(s.bot_id)


def test_closing_fallback_silent_in_short_meeting(tmp_path, monkeypatch):
    """Same lull, but the meeting is only 60s old → the duration gate keeps her
    quiet (no false wrap-up in a short call)."""
    s = _seed_wrapup_session(
        tmp_path, monkeypatch, "nudge-fallback-short", meeting_age=60, idle=30
    )
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    body = _post(_line_payload(s.bot_id, "Duccio", "so, where does that leave everyone?"))
    assert body.get("quiet_nudge") is None
    assert not spoken
    store.remove(s.bot_id)


def test_closing_fallback_silent_in_active_meeting(tmp_path, monkeypatch):
    """Long meeting, but the room is actively talking (last line 4s ago) → the
    idle gate keeps her quiet."""
    s = _seed_wrapup_session(
        tmp_path, monkeypatch, "nudge-fallback-active", meeting_age=600, idle=4
    )
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    body = _post(_line_payload(s.bot_id, "Duccio", "so, where does that leave everyone?"))
    assert body.get("quiet_nudge") is None
    assert not spoken
    store.remove(s.bot_id)


def test_exact_closing_phrase_still_nudges_when_fallback_disabled(tmp_path, monkeypatch):
    """Regression: the regex path is untouched — an exact closing phrase nudges
    even with the fallback turned off and a young, active meeting."""
    from app.config import settings

    s = _seed_wrapup_session(
        tmp_path, monkeypatch, "nudge-regex-only", meeting_age=30, idle=2
    )
    monkeypatch.setattr(settings, "closing_fallback_enabled", False)
    spoken = []

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    body = _post(_line_payload(s.bot_id, "Duccio", "okay, anything else before we wrap up?"))
    assert body.get("quiet_nudge") is True
    assert spoken and "Anna" in spoken[0]
    store.remove(s.bot_id)


# ── BLOCKER 2: a directly-addressed turn after a lull must NOT pay for the
# synchronous proactive model call (latency in front of her first token). The
# proactive/closing check is computed only for UNADDRESSED lulls. ──


def test_called_line_after_lull_skips_proactive_path(tmp_path, monkeypatch):
    """>3min meeting, >25s lull, but the line addresses Laura by name → the
    proactive check (retrieve + LLM) is never invoked; the fast answer path owns
    the turn."""
    from app.config import settings

    calls = {"n": 0}

    def spy_proactive(*a, **k):
        calls["n"] += 1
        return {"should_speak": False, "line": "", "confidence": 0.0}

    monkeypatch.setattr(main, "proactive_flag", spy_proactive)

    def instant_answer(*a, **k):
        yield "The next step is the security review."

    monkeypatch.setattr(main, "answer_question_stream", instant_answer)

    async def fake_speak(session, line, citations=None, **kw):
        return True

    async def fake_speak_audio(session, text, *, force, generation, prev, t0=None):
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)
    monkeypatch.setattr(main, "_speak_with_audio", fake_speak_audio)

    s = _seed_wrapup_session(
        tmp_path, monkeypatch, "called-lull", meeting_age=300, idle=30
    )
    monkeypatch.setattr(settings, "proactive_enabled", True)  # helper disabled it

    _post(_line_payload(s.bot_id, "Duccio", "Laura, what's the next step?"))
    assert calls["n"] == 0  # proactive_flag never ran on a called turn
    store.remove(s.bot_id)


def test_unaddressed_lull_still_enters_proactive_path(tmp_path, monkeypatch):
    """The counterpart: an UNADDRESSED lull in the same long meeting still runs
    the proactive check — the fallback behavior is preserved for room talk."""
    from app.config import settings

    calls = {"n": 0}

    def spy_proactive(*a, **k):
        calls["n"] += 1
        return {"should_speak": False, "line": "", "confidence": 0.0}

    monkeypatch.setattr(main, "proactive_flag", spy_proactive)

    async def fake_speak(session, line, citations=None, **kw):
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)

    s = _seed_wrapup_session(
        tmp_path, monkeypatch, "unaddr-lull", meeting_age=300, idle=30
    )
    monkeypatch.setattr(settings, "proactive_enabled", True)

    _post(_line_payload(s.bot_id, "Duccio", "so, where does that leave everyone?"))
    assert calls["n"] == 1  # proactive check ran on the unaddressed lull
    store.remove(s.bot_id)

