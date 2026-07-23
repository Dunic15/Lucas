"""During-meeting fluidity (spec 2026-07-22): follow-up window shape+speaker
binding, and live corrections on the active action draft. Key-free.

Written TEST-FIRST against the live-repro failures:
  - an imperative follow-up ("crea una task") needed her name re-said, because
    the window's shape gate was a hard endswith('?');
  - ANY participant's bare question rode the window, not just the person she
    was actually talking to;
  - a spoken correction ("no, not Anant — Marco") minted a second card or got
    APPENDED to the draft instead of replacing the wrong value;
  - "lascia perdere" stopped her voice but the draft card survived to the
    dashboard anyway.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time as _time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402


def _session(tmp_path, monkeypatch, bot_id="fluidity-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/flu-idty-tst", "laura")
    s.addressed_once = True  # meeting underway; first-call gate covered elsewhere
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


def _post(payload: dict) -> dict:
    class FakeRequest:
        headers: dict = {}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    resp = asyncio.run(main.recall_webhook(FakeRequest()))
    return json.loads(resp.body)


def _mute(monkeypatch, spoken: list) -> None:
    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)


def _instant_answer(monkeypatch) -> None:
    def gen(*a, **k):
        yield "The DPA comes right after the security review."

    monkeypatch.setattr(main, "answer_question_stream", gen)


def _group(s: store.Session) -> None:
    """3 humans: keeps 1:1 relaxations out of these scenarios."""
    s.participant_event("Duccio", 1, here=True)
    s.participant_event("Marco", 2, here=True)
    s.participant_event("Anna", 3, here=True)


def _serve_duccio(s, monkeypatch, spoken) -> None:
    """Duccio asks by name and she answers: the follow-up window is now HIS."""
    _instant_answer(monkeypatch)
    _mute(monkeypatch, spoken)
    body = _post(_line(s.bot_id, "Duccio", 1, "Laura, what's the next onboarding step?"))
    assert spoken, f"setup: the named ask must be answered, got {body}"
    s.mark_spoke()  # the fake speak doesn't stamp the cooldown clock


# ── follow-up window: shape (imperatives ride) ──────────────────────────


def test_followup_imperative_is_captured_without_wake_word(tmp_path, monkeypatch):
    """'crea una task per il recap' right after her answer, same speaker, no
    name and no question mark → it's a follow-up ask, not unaddressed noise:
    it must reach the capture seam (one queued card), not die in cooldown."""
    s = _session(tmp_path, monkeypatch, bot_id="flu-imperative-1")
    s.memory_brief = ""
    _group(s)
    spoken: list = []
    _serve_duccio(s, monkeypatch, spoken)
    spoken.clear()

    body = _post(_line(s.bot_id, "Duccio", 1, "crea una task per preparare il recap"))
    queued = getattr(s, "queued_actions", None) or []
    assert body.get("reason") != "cooldown", body
    assert len(queued) == 1, f"imperative follow-up must capture once, got {queued}"
    store.remove(s.bot_id)


def test_followup_statement_still_respects_cooldown(tmp_path, monkeypatch):
    """Guard: relaxing the '?' gate must NOT open the window to commentary."""
    s = _session(tmp_path, monkeypatch, bot_id="flu-statement-1")
    s.memory_brief = ""
    _group(s)
    spoken: list = []
    _serve_duccio(s, monkeypatch, spoken)

    body = _post(_line(s.bot_id, "Duccio", 1, "ok that makes sense to me"))
    assert body.get("reason") == "cooldown", body
    store.remove(s.bot_id)


# ── follow-up window: speaker binding ───────────────────────────────────


def test_followup_from_other_speaker_defers_to_room(tmp_path, monkeypatch):
    """Duccio was being served; MARCO's bare question inside the window is a
    room question, not her follow-up: it takes the deference path (and yields
    when a human picks it up) instead of the fast lane."""
    s = _session(tmp_path, monkeypatch, bot_id="flu-otherspeaker-1")
    s.memory_brief = ""
    _group(s)
    spoken: list = []
    _serve_duccio(s, monkeypatch, spoken)
    spoken.clear()

    monkeypatch.setattr(settings, "deference_seconds", 0.05)
    real_sleep = asyncio.sleep

    async def sleep_and_interject(seconds):
        s.add_utterance("Anna", "it lands on Friday I think")
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", sleep_and_interject)
    body = _post(_line(s.bot_id, "Marco", 2, "e la scadenza?"))
    # Either throttle is a correct degradation — the claim under test is that
    # the FAST LANE (immediate answer) did not fire for the other speaker.
    assert body.get("reason") in ("cooldown", "deferred to human"), body
    assert not spoken, "another speaker's bare question must not fast-path"
    store.remove(s.bot_id)


def test_followup_same_speaker_question_still_fast(tmp_path, monkeypatch):
    """Regression guard: the served speaker's own bare question keeps the
    fast lane (no cooldown, no deference)."""
    s = _session(tmp_path, monkeypatch, bot_id="flu-samespeaker-1")
    s.memory_brief = ""
    _group(s)
    spoken: list = []
    _serve_duccio(s, monkeypatch, spoken)
    spoken.clear()

    monkeypatch.setattr(settings, "deference_seconds", 30.0)  # would hang if hit
    body = _post(_line(s.bot_id, "Duccio", 1, "and what about the deadline?"))
    assert body.get("reason") not in ("cooldown", "deferred to human"), body
    assert spoken, "same-speaker follow-up must be answered"
    store.remove(s.bot_id)


# ── live corrections: replace, never a second card ──────────────────────


def _capture(s, monkeypatch, spoken, text) -> list:
    _mute(monkeypatch, spoken)
    body = _post(_line(s.bot_id, "Duccio", 1, text))
    queued = getattr(s, "queued_actions", None) or []
    assert len(queued) == 1, f"setup: expected one captured card, got {body}"
    return queued


def test_recipient_correction_replaces_not_duplicates(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="flu-correct-1")
    s.memory_brief = ""
    _group(s)
    spoken: list = []
    _capture(s, monkeypatch, spoken, "Laura, send the recap email to Anant")

    _post(_line(s.bot_id, "Duccio", 1, "no, not Anant, Marco"))
    queued = getattr(s, "queued_actions", None) or []
    assert len(queued) == 1, f"a correction must never mint a second card: {queued}"
    action = queued[0].get("action") or ""
    assert "Marco" in action, action
    assert "Anant" not in action, f"old recipient must be replaced, got: {action}"
    store.remove(s.bot_id)


def test_date_correction_replaces_in_italian(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="flu-correct-2")
    s.memory_brief = ""
    _group(s)
    spoken: list = []
    _capture(s, monkeypatch, spoken, "Laura, manda una mail di recap ad Anant venerdì")

    _post(_line(s.bot_id, "Duccio", 1, "no, non venerdì, lunedì"))
    queued = getattr(s, "queued_actions", None) or []
    assert len(queued) == 1, queued
    action = (queued[0].get("action") or "").lower()
    assert "lunedì" in action, action
    assert "venerdì" not in action, f"old date must be replaced, got: {action}"
    store.remove(s.bot_id)


def test_cancel_discards_the_pending_draft(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="flu-cancel-1")
    s.memory_brief = ""
    _group(s)
    spoken: list = []
    _capture(s, monkeypatch, spoken, "Laura, send the recap email to Anant")

    body = _post(_line(s.bot_id, "Duccio", 1, "lascia perdere"))
    queued = getattr(s, "queued_actions", None) or []
    assert not queued, f"cancelled draft must not survive to the dashboard: {queued}"
    assert getattr(s, "pending_clarify", None) is None
    assert body.get("action_cancelled") is True, body
    store.remove(s.bot_id)


@pytest.mark.parametrize(
    "phrase",
    ["remove that task", "delete that action", "discard that", "elimina",
     "eliminala", "rimuovila", "toglila", "cancel that", "never mind",
     "lascia perdere"],
)
def test_cancel_phrases_are_recognised(phrase):
    from app.brain import tools
    assert tools.is_draft_cancel(phrase), phrase


@pytest.mark.parametrize(
    "phrase",
    ["remove the blocker from the doc", "delete the old file please",
     "let us discuss the roadmap", "send an email to Sofia",
     "schedule a call tomorrow"],
)
def test_normal_talk_is_not_a_cancel(phrase):
    from app.brain import tools
    assert not tools.is_draft_cancel(phrase), phrase


def test_correction_from_unrelated_speaker_is_ignored(tmp_path, monkeypatch):
    """Cross-talk guard: someone ELSE's 'no, not X, Y' must not rewrite the
    asker's draft."""
    s = _session(tmp_path, monkeypatch, bot_id="flu-correct-guard-1")
    s.memory_brief = ""
    _group(s)
    spoken: list = []
    _capture(s, monkeypatch, spoken, "Laura, send the recap email to Anant")

    _post(_line(s.bot_id, "Marco", 2, "no, not Anant, Marco"))
    queued = getattr(s, "queued_actions", None) or []
    assert len(queued) == 1
    assert "Anant" in (queued[0].get("action") or ""), queued
    store.remove(s.bot_id)


def test_correction_retry_is_idempotent(tmp_path, monkeypatch):
    """Recall re-delivers finals: the SAME correction twice applies once."""
    s = _session(tmp_path, monkeypatch, bot_id="flu-correct-retry-1")
    s.memory_brief = ""
    _group(s)
    spoken: list = []
    _capture(s, monkeypatch, spoken, "Laura, send the recap email to Anant")

    payload = _line(s.bot_id, "Duccio", 1, "no, not Anant, Marco")
    _post(payload)
    _post(payload)  # verbatim Recall retry
    queued = getattr(s, "queued_actions", None) or []
    assert len(queued) == 1
    action = queued[0].get("action") or ""
    assert action.count("Marco") == 1, f"retry must not re-apply: {action}"
    store.remove(s.bot_id)

def test_late_search_result_raises_and_is_delivered(tmp_path, monkeypatch):
    """A requested web result survives ordinary multiparty chatter.

    The search announce speaks immediately. A newer human turn advances the
    normal speech generation while search runs; the answer must be queued behind
    a raised hand, then delivered when the asker says "did you find it?".
    """
    s = _session(tmp_path, monkeypatch, bot_id="late-search-result-1")
    s.memory_brief = ""
    _group(s)
    spoken: list[str] = []
    _mute(monkeypatch, spoken)
    monkeypatch.setattr(settings, "recall_api_key", "")
    monkeypatch.setattr(main, "wants_web_search", lambda _text: True)

    def search_stream(*args, **kwargs):
        yield "One moment — let me look that up online."
        # A human spoke while the provider was working.
        store.bump_speech_generation(s)
        yield "From a quick search, the Y Combinator deadline is August 4."

    monkeypatch.setattr(main, "answer_question_stream", search_stream)

    body = _post(_line(
        s.bot_id, "Duccio", 1,
        "Laura, search online for the Y Combinator application deadline",
    ))
    assert body.get("search_ready") is True, body
    assert body.get("hand_raised") is True, body
    assert s.pending_contribution_kind == "search"
    assert "August 4" in s.pending_contribution

    delivered = _post(_line(
        s.bot_id, "Duccio", 1,
        "Laura, did you find the Y Combinator application deadline?",
    ))
    assert delivered.get("hand_delivered") is True, delivered
    assert delivered.get("search_result") is True, delivered
    assert any("August 4" in line for line in spoken)
    assert not s.hand_raised_at
    assert not s.pending_contribution
    store.remove(s.bot_id)


@pytest.mark.parametrize(
    "phrase",
    [
        "did you find it?",
        "what did you find?",
        "any results?",
        "hai trovato qualcosa?",
    ],
)
def test_search_result_followups_are_recognised(phrase):
    assert main._is_search_result_request(phrase)
