"""Address-only group mode (owner rule 2026-07-29): with several humans in the
room the avatar speaks ONLY when addressed by name — no follow-up window, no
deference answers, no spoken nudges — and in-call research goes to meeting
chat instead of voice. Key-free."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402


def _session(tmp_path, monkeypatch, bot_id="grp-bot", humans=("Ben", "Marco")) -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    s.addressed_once = True
    for i, name in enumerate(humans, start=1):
        s.participant_event(name, i, here=True)
    monkeypatch.setattr(settings, "deference_seconds", 0)
    monkeypatch.setattr(settings, "recall_api_key", "")
    # Opt IN to the feature under test (conftest baselines it off for the
    # legacy suite).
    monkeypatch.setattr(settings, "address_only_min_humans", 2)
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

    async def fake_speak_with_audio(
        session, text, *, force, generation, prev, t0=None, gate=None
    ):
        spoken.append(text)
        return True

    monkeypatch.setattr(main, "_speak_with_audio", fake_speak_with_audio)
    return spoken


def test_unaddressed_group_line_is_silent(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    _stub_stream(monkeypatch, ["The onboarding window is two weeks."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "how long should we plan for onboarding?"))
    assert body.get("spoke") is False
    assert body.get("reason") == "not addressed (group)"
    assert not spoken
    assert s.hand_raised_at == 0.0  # no hand either: pure silent listening
    store.remove(s.bot_id)


def test_direct_address_in_group_still_answers(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="grp-called")
    monkeypatch.setattr(settings, "ack_enabled", False)
    _stub_stream(monkeypatch, ["Two weeks is the usual onboarding window."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "Laura, how long is onboarding?"))
    assert body.get("spoke") is True
    assert spoken and "Two weeks" in spoken[0]
    store.remove(s.bot_id)


def test_followup_without_name_is_silent_in_group(tmp_path, monkeypatch):
    """Owner decision: name required ALWAYS in group rooms — a question right
    after her own answer no longer rides the follow-up window."""
    s = _session(tmp_path, monkeypatch, bot_id="grp-followup")
    s.mark_spoke()  # she JUST answered something
    _stub_stream(monkeypatch, ["The deadline is Friday."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "and what about the deadline?"))
    assert body.get("spoke") is False
    assert body.get("reason") == "not addressed (group)"
    assert not spoken
    store.remove(s.bot_id)


def test_one_to_one_keeps_fluent_behaviour(tmp_path, monkeypatch):
    """A single human room is below the threshold: unaddressed groundable
    questions still answer, exactly as before."""
    s = _session(tmp_path, monkeypatch, bot_id="grp-1to1", humans=("Ben",))
    monkeypatch.setattr(settings, "ack_enabled", False)
    _stub_stream(monkeypatch, ["Two weeks is the usual window."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "how long should we plan for onboarding?"))
    assert body.get("spoke") is True
    assert spoken
    store.remove(s.bot_id)


def test_disabled_restores_legacy_group_behaviour(tmp_path, monkeypatch):
    """address_only_min_humans=0 turns the mode off: the unaddressed line
    reaches the legacy multi-party path (here: answered, hand-raise etc.)."""
    s = _session(tmp_path, monkeypatch, bot_id="grp-off")
    monkeypatch.setattr(settings, "address_only_min_humans", 0)
    monkeypatch.setattr(settings, "ack_enabled", False)
    _stub_stream(monkeypatch, ["The onboarding window is two weeks."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "how long should we plan for onboarding?"))
    # Legacy path: 2 humans is below hand_raise_min_humans (3), so she answers.
    assert body.get("spoke") is True
    assert spoken
    store.remove(s.bot_id)


# ── research → meeting chat ──


def test_search_question_acks_and_posts_to_chat(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="grp-search")
    monkeypatch.setattr(settings, "search_results_to_chat", True)
    # wants_web_search is gated on the Anthropic key (search runs on Claude).
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", True)
    # A "real" bot so the path is live; the keyed webhook demands a realtime
    # capability, satisfied via a stubbed resolver + `cap` query param below.
    monkeypatch.setattr(settings, "recall_api_key", "k")
    monkeypatch.setattr(
        store, "resolve_recall_realtime_capability", lambda cap: s.bot_id
    )

    posted: list[str] = []
    spoken: list[str] = []
    monkeypatch.setattr(main, "web_search_answer", lambda q, c="": "Latest figure: 42%.")
    monkeypatch.setattr(
        main, "_post_to_meeting_chat", lambda session, text: posted.append(text)
    )

    async def fake_speak(session, line, citations=None, **kw):
        spoken.append(line)
        return True

    monkeypatch.setattr(main, "_make_avatar_speak", fake_speak)

    payload = _line(s.bot_id, "Ben", "Laura, can you search the latest churn benchmark?")

    class FakeRequest:
        headers: dict = {}
        query_params: dict = {"cap": "t"}

        async def body(self) -> bytes:
            return json.dumps(payload).encode()

    async def call() -> dict:
        resp = await main.recall_webhook(FakeRequest())
        for _ in range(20):
            if posted:
                break
            await asyncio.sleep(0.05)
        return json.loads(resp.body)

    body = asyncio.run(call())
    assert body.get("search_to_chat") is True
    assert spoken and "chat" in spoken[0].lower()  # the one spoken ack line
    assert posted == ["Latest figure: 42%."]
    store.remove(s.bot_id)


def test_search_keyfree_falls_back_to_spoken_stream(tmp_path, monkeypatch):
    """Without a Recall key (demo) there is no chat to post to: the search
    question keeps today's announced streamed spoken answer."""
    s = _session(tmp_path, monkeypatch, bot_id="grp-search-demo")
    monkeypatch.setattr(settings, "ack_enabled", False)
    _stub_stream(monkeypatch, ["Let me look that up.", "It's 42%."])
    spoken = _capture_speech(monkeypatch)

    body = _post(_line(s.bot_id, "Ben", "Laura, can you search the latest churn benchmark?"))
    assert body.get("search_to_chat") is None
    assert body.get("spoke") is True
    assert len(spoken) == 2
    store.remove(s.bot_id)
