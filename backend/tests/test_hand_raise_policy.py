"""Hand-raise motivation gate (Inner Thoughts-lite): the SKIP gate decides if a
contribution is grounded; this policy decides if RAISING for it is socially
worth it — near-dup guard, per-meeting budget, pacing, back-off after the room
ignored her. No keys, no model."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.decision import should_raise_hand, similar_contribution  # noqa: E402


# ── the pure policy ──

_KW = dict(max_per_meeting=4, min_gap_seconds=90.0, ignored_gap_seconds=240.0)


def test_first_raise_is_free():
    assert should_raise_hand(now=1000.0, count=0, last_at=0.0,
                             last_ignored=False, **_KW)


def test_budget_caps_the_meeting():
    assert not should_raise_hand(now=1e9, count=4, last_at=0.0,
                                 last_ignored=False, **_KW)


def test_min_gap_paces_consecutive_raises():
    assert not should_raise_hand(now=1060.0, count=1, last_at=1000.0,
                                 last_ignored=False, **_KW)   # 60s < 90s
    assert should_raise_hand(now=1100.0, count=1, last_at=1000.0,
                             last_ignored=False, **_KW)       # 100s >= 90s


def test_ignored_hand_backs_off_longer():
    # 100s would clear the normal gap, but the room ignored the last raise:
    # silence means "not now" — wait ignored_gap_seconds instead.
    assert not should_raise_hand(now=1100.0, count=1, last_at=1000.0,
                                 last_ignored=True, **_KW)
    assert should_raise_hand(now=1250.0, count=1, last_at=1000.0,
                             last_ignored=True, **_KW)


def test_similar_contribution_catches_same_point():
    a = "Onboarding usually takes two weeks with security review included."
    assert similar_contribution(a, a)
    assert similar_contribution(
        a, "Onboarding usually takes two weeks, with the security review included."
    )


def test_similar_contribution_lets_new_points_through():
    a = "Onboarding usually takes two weeks with security review included."
    b = "The pricing tier for enterprise starts at nine hundred a month."
    assert not similar_contribution(a, b)
    assert not similar_contribution(a, "")
    assert not similar_contribution("", "")


# ── live path (webhook) ──


def _session(tmp_path, monkeypatch, bot_id="policy-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    s.addressed_once = True
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


def _stub_stream(monkeypatch, sentences):
    def stream(*a, **k):
        yield from sentences

    monkeypatch.setattr(main, "answer_question_stream", stream)


def test_same_point_never_raises_twice(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    _stub_stream(monkeypatch, ["Onboarding takes two weeks with security review."])

    body = _post(_line(s.bot_id, "Ben", "how long does onboarding take?"))
    assert body.get("hand_raised") is True

    # Hand times out (ignored), the SAME grounded point comes back from the
    # stream on later talk: suppressed as a duplicate, not re-raised.
    s.hand_raised_at = time.time() - settings.hand_raise_timeout_seconds - 1
    _post(_line(s.bot_id, "Marco", "moving on to the next item"))  # timeout sweep
    assert s.hand_raised_at == 0
    body = _post(_line(s.bot_id, "Ben", "anyway, about onboarding timing again"))
    assert body.get("reason") == "hand suppressed (same point)"
    assert s.hand_raise_count == 1  # no second raise happened
    store.remove(s.bot_id)


def test_budget_and_pacing_suppress_rapid_new_points(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="policy-bot-2")
    _stub_stream(monkeypatch, ["A brand new point about pricing tiers."])
    # She raised 30s ago (delivered path, so not ignored): min gap not met.
    s.hand_raise_count = 1
    s.hand_last_raise_at = time.time() - 30
    s.hand_last_ignored = False
    s.hand_last_contribution = "Onboarding takes two weeks."

    body = _post(_line(s.bot_id, "Ben", "what about the pricing side?"))
    assert body.get("reason") == "hand suppressed (budget)"
    assert s.hand_raise_count == 1
    store.remove(s.bot_id)


def test_ignored_raise_backs_off_but_engaged_resets(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="policy-bot-3")
    _stub_stream(monkeypatch, ["Fresh grounded point on the rollout plan."])
    # Last raise 2 minutes ago and the room IGNORED it -> still backing off.
    s.hand_raise_count = 1
    s.hand_last_raise_at = time.time() - 120
    s.hand_last_ignored = True
    s.hand_last_contribution = "Something about onboarding."

    body = _post(_line(s.bot_id, "Ben", "let's discuss the rollout"))
    assert body.get("reason") == "hand suppressed (budget)"

    # But when the room ENGAGES her hand (invite), the back-off resets.
    s.hand_raised_at = time.time()
    s.pending_contribution = "Queued point."

    async def fake_speak(session, text, *, force, generation, prev, t0=None):
        return True

    monkeypatch.setattr(main, "_speak_with_audio", fake_speak)
    body = _post(_line(s.bot_id, "Ben", "Laura, dimmi"))
    assert body.get("hand_delivered") is True
    assert s.hand_last_ignored is False
    store.remove(s.bot_id)


def test_hard_cap_per_meeting(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="policy-bot-4")
    _stub_stream(monkeypatch, ["Yet another completely different novel point."])
    s.hand_raise_count = settings.hand_raise_max_per_meeting
    s.hand_last_raise_at = time.time() - 9999  # pacing satisfied
    s.hand_last_ignored = False
    s.hand_last_contribution = "old other point"

    body = _post(_line(s.bot_id, "Ben", "one more topic to cover"))
    assert body.get("reason") == "hand suppressed (budget)"
    store.remove(s.bot_id)
