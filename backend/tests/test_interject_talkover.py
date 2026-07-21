"""Talk-over guard for the high-confidence interjection escape
(settings.interject_recheck_floor_at_speak).

In hand_mode the whole contribution is generated (deference sleep + full answer
collected; several seconds) BEFORE the floor-open check. `last_human_partial_at`
lags (written only on partials), so a trigger-time "floor open" reading can be
stale by the time she's ready to speak. The floor decision is re-checked at SPEAK
time with two extra "someone is (or just was) talking" signals: a new transcript
line landed during generation, and a human partial arrived during the generation
window. A genuinely open floor still interjects. No keys, no model.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.decision import interjection_floor_open  # noqa: E402


# ── pure decision function: the extra floor-busy signals ──


def test_default_off_signals_preserve_original_decision():
    # Defaults (transcript_grew False, generation_elapsed None) reproduce the
    # original decision exactly; a finished line with no recent partial is open.
    assert interjection_floor_open(
        turn_completeness=0.9, since_human_partial=10.0, active_partial_seconds=0.6
    )


def test_transcript_growth_closes_the_floor():
    # A finished line + no recent partial would read OPEN, but a NEW transcript
    # line landed while she was generating → a human took the floor → defer.
    assert not interjection_floor_open(
        turn_completeness=0.95,
        since_human_partial=30.0,
        active_partial_seconds=1.0,
        transcript_grew=True,
    )


def test_partial_during_generation_closes_the_floor():
    # The last partial (1.2s ago) is OLDER than active_partial_seconds (1.0), so
    # the trigger-time check reads open; but it arrived DURING her 2s generation
    # window (1.2 < 2.0), i.e. someone spoke while she generated → defer.
    assert not interjection_floor_open(
        turn_completeness=0.95,
        since_human_partial=1.2,
        active_partial_seconds=1.0,
        generation_elapsed=2.0,
    )


def test_partial_predating_the_turn_keeps_the_floor_open():
    # A partial OLDER than the whole generation window (3.0 > 2.0) predates the
    # turn; it is not during-turn activity, so the floor stays OPEN (a genuine
    # lull still interjects; this is what stops the guard from over-deferring).
    assert interjection_floor_open(
        turn_completeness=0.95,
        since_human_partial=3.0,
        active_partial_seconds=1.0,
        generation_elapsed=2.0,
    )


# ── live path (webhook): the hand-raise interjection escape ──


def _session(tmp_path, monkeypatch, bot_id="talkover-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    s.addressed_once = True  # already activated (first-call gate has its own tests)
    s.participant_event("Ben", 1, here=True)
    s.participant_event("Marco", 2, here=True)
    s.participant_event("Sara", 3, here=True)  # >3 participants → hand_mode
    monkeypatch.setattr(settings, "deference_seconds", 0)
    monkeypatch.setattr(settings, "recall_api_key", "")
    return s


def _line(bot_id: str, speaker: str, text: str) -> dict:
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


def _post(payload: dict) -> dict:
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    resp = asyncio.run(main.recall_webhook(FakeRequest()))
    return json.loads(resp.body)


def _capture_speech(monkeypatch) -> list:
    spoken: list = []

    async def fake_speak_with_audio(session, text, *, force, generation, prev, t0=None):
        spoken.append(text)
        return True

    monkeypatch.setattr(main, "_speak_with_audio", fake_speak_with_audio)
    return spoken


def _stub_stream(monkeypatch, session, sentences, *, top_score, grow=False):
    """Fake answer stream. When ``grow`` is set it appends a human line to the
    session AS IT FINISHES generating; simulating a human final that landed
    mid-turn (the floor closing while she generated her contribution)."""

    def stream(*a, **k):
        if k.get("meta") is not None:
            k["meta"]["top_score"] = top_score
        for sent in sentences:
            yield sent
        if grow:
            session.add_utterance("Marco", "actually hold on, let me add something")

    monkeypatch.setattr(main, "answer_question_stream", stream)


def test_open_floor_still_interjects(tmp_path, monkeypatch):
    """No new line, no partial during generation, strongly grounded, finished
    line → she says ONE line directly (the guard does not over-defer)."""
    s = _session(tmp_path, monkeypatch, bot_id="talkover-open")
    s.last_human_partial_at = time.time() - 30.0  # nobody talking
    _stub_stream(
        monkeypatch,
        s,
        ["The onboarding SOP puts security review before access provisioning."],
        top_score=0.9,
    )
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "what comes before we grant access?"))
    assert body.get("interjected") is True
    assert body.get("spoke") is True
    assert spoken and "security review" in spoken[0]
    assert s.hand_raised_at == 0  # she spoke; no hand up
    store.remove(s.bot_id)


def test_transcript_grew_during_generation_defers(tmp_path, monkeypatch):
    """last_human_partial_at is STALE (30s ago); the trigger-time floor read
    says OPEN; but a human final landed while she generated. She must NOT talk
    over them: defer to the raised hand instead."""
    s = _session(tmp_path, monkeypatch, bot_id="talkover-grew")
    s.last_human_partial_at = time.time() - 30.0  # stale "all clear"
    _stub_stream(
        monkeypatch,
        s,
        ["The onboarding SOP puts security review before access provisioning."],
        top_score=0.9,  # strongly grounded: WOULD interject on an open floor
        grow=True,  # a human took the floor mid-generation
    )
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "what comes before we grant access?"))
    assert body.get("hand_raised") is True
    assert body.get("interjected") is None
    assert not spoken  # no talk-over
    assert s.hand_raised_at > 0
    store.remove(s.bot_id)


def test_flag_off_restores_trigger_time_reading(tmp_path, monkeypatch):
    """With the recheck flag OFF, the stale trigger-time read wins and she
    interjects even though a human took the floor mid-generation (the pre-fix
    behaviour) — proving the fix is fully gated."""
    s = _session(tmp_path, monkeypatch, bot_id="talkover-flagoff")
    monkeypatch.setattr(settings, "interject_recheck_floor_at_speak", False)
    s.last_human_partial_at = time.time() - 30.0
    _stub_stream(
        monkeypatch,
        s,
        ["The onboarding SOP puts security review before access provisioning."],
        top_score=0.9,
        grow=True,
    )
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "what comes before we grant access?"))
    assert body.get("interjected") is True  # stale read → talks over (pre-fix)
    assert spoken
    store.remove(s.bot_id)
