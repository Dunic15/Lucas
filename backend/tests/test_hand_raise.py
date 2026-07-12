"""Hand-raise etiquette: in a multi-human meeting, an unaddressed grounded
contribution is queued behind a raised hand (gesture + chat) instead of spoken
over the room; an invite ("dimmi, Laura") delivers it, a timeout drops it.
No keys, no model."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.decision import detect_invite  # noqa: E402


# ── invite detection (decision.py) ──

INVITES = [
    "dimmi",
    "dimmi pure",
    "sì, dimmi",
    "cosa c'è?",
    "che c'è",
    "vai pure",
    "vai",
    "prego",
    "sentiamo",
    "go ahead",
    "okay go ahead",
    "tell us",
    "what's up?",
    "we're listening",
    "would you like to add anything?",
    "anything to add?",
    "do you want to add something",
    "vuoi aggiungere qualcosa",
]

NOT_INVITES = [
    "",
    "dimmi il budget per il Q3",  # a request, not a floor hand-off
    "what's the budget?",
    "vai avanti col deploy",
    "parla con Marco dopo la call",
    "tell us about the onboarding process",
]


def test_detect_invite_positive():
    for line in INVITES:
        assert detect_invite(line), f"should invite: {line!r}"


def test_detect_invite_negative():
    for line in NOT_INVITES:
        assert not detect_invite(line), f"must NOT invite: {line!r}"


# ── live path (webhook) ──


def _session(tmp_path, monkeypatch, bot_id="hand-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    s.addressed_once = True  # already activated (first-call gate has its own tests)
    # A multi-human room — the hand-raise only applies with >= min_humans.
    s.participant_event("Ben", 1, here=True)
    s.participant_event("Marco", 2, here=True)
    # Deterministic + offline: no deference wait, no real Recall chat call.
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


def _stub_stream(monkeypatch, sentences):
    def stream(*a, **k):
        yield from sentences

    monkeypatch.setattr(main, "answer_question_stream", stream)


def _capture_speech(monkeypatch):
    spoken = []

    async def fake_speak_with_audio(session, text, *, force, generation, prev, t0=None):
        spoken.append(text)
        return True

    monkeypatch.setattr(main, "_speak_with_audio", fake_speak_with_audio)
    return spoken


def test_unaddressed_contribution_raises_hand_instead_of_speaking(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    _stub_stream(monkeypatch, ["I'd flag that onboarding usually takes two weeks."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "how long should we plan for onboarding?"))
    assert body.get("hand_raised") is True
    assert body.get("spoke") is False
    assert not spoken  # the contribution was queued, not spoken
    assert s.hand_raised_at > 0
    assert "onboarding" in s.pending_contribution
    assert any(m.get("type") == "raise_hand" for m in s.pending_messages)
    store.remove(s.bot_id)


def test_no_hand_raise_in_one_to_one(tmp_path, monkeypatch):
    """With a single human in the room she answers directly, as before."""
    s = _session(tmp_path, monkeypatch, bot_id="hand-bot-1to1")
    s.participant_event("Marco", 2, here=False)  # Ben is alone with her
    _stub_stream(monkeypatch, ["Two weeks is the usual onboarding window."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "how long should we plan for onboarding?"))
    assert body.get("hand_raised") is None
    assert spoken  # direct answer
    assert s.hand_raised_at == 0
    store.remove(s.bot_id)


def test_hand_raised_holds_further_contributions(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="hand-bot-hold")
    s.hand_raised_at = time.time()
    s.pending_contribution = "My queued point."
    _stub_stream(monkeypatch, ["A second point that must NOT be generated."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "and what about the pricing tiers?"))
    assert body.get("reason") == "hand raised"
    assert not spoken
    assert s.pending_contribution == "My queued point."  # first point untouched
    store.remove(s.bot_id)


def test_invite_delivers_queued_contribution(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="hand-bot-invite")
    s.hand_raised_at = time.time()
    s.pending_contribution = "I'd flag the onboarding timeline."
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "Laura, dimmi"))
    assert body.get("hand_delivered") is True
    assert spoken == ["I'd flag the onboarding timeline."]
    assert s.hand_raised_at == 0  # hand came down
    assert s.pending_contribution == ""
    assert any(m.get("type") == "lower_hand" for m in s.pending_messages)
    store.remove(s.bot_id)


def test_bare_address_also_delivers(tmp_path, monkeypatch):
    """A bare "Laura?" while her hand is up is the natural invite."""
    s = _session(tmp_path, monkeypatch, bot_id="hand-bot-bare")
    s.hand_raised_at = time.time()
    s.pending_contribution = "Quick point on the budget."
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "Laura?"))
    assert body.get("hand_delivered") is True
    assert spoken == ["Quick point on the budget."]
    store.remove(s.bot_id)


def test_substantive_ask_lowers_hand_and_answers_normally(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="hand-bot-ask")
    s.hand_raised_at = time.time()
    s.pending_contribution = "The stale queued point."
    _stub_stream(monkeypatch, ["The deadline is Friday."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "Laura, when is the deadline again?"))
    assert body.get("hand_delivered") is None
    assert spoken == ["The deadline is Friday."]  # the ask wins, not the queue
    assert s.hand_raised_at == 0
    assert s.pending_contribution == ""
    store.remove(s.bot_id)


def test_hand_times_out_silently(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="hand-bot-timeout")
    s.hand_raised_at = time.time() - settings.hand_raise_timeout_seconds - 1
    s.pending_contribution = "A point whose moment has passed."
    _stub_stream(monkeypatch, [])  # SKIP: nothing new to contribute either
    spoken = _capture_speech(monkeypatch)

    _post(_line(s.bot_id, "Ben", "moving on to the next agenda item"))
    assert s.hand_raised_at == 0
    assert s.pending_contribution == ""
    assert not spoken  # dropped silently — never delivered late
    assert any(m.get("type") == "lower_hand" for m in s.pending_messages)
    store.remove(s.bot_id)
