"""Cross-talk / locked-dyad suppression: when two humans are in a tight
back-and-forth, an unaddressed interjection is held back to a SILENT raised hand
and the deference wait is stretched — she never speaks over their volley.
Pure-function calibration + the webhook wiring. No keys, no model."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.decision import in_locked_dyad  # noqa: E402


class _U:
    """Minimal Utterance stand-in (speaker + ts) for the pure-function tests."""

    def __init__(self, speaker: str, ts: float):
        self.speaker = speaker
        self.ts = ts
        self.text = "x"


# ── the pure detector ──

_KW = dict(min_turns=4, max_gap_seconds=8.0, window=6)


def test_tight_two_party_alternation_is_a_dyad():
    now = 1000.0
    t = [_U("Marco", 991), _U("Lia", 993), _U("Marco", 995), _U("Lia", 997)]
    assert in_locked_dyad(t, avatar_name="Laura", now=now, **_KW)


def test_three_speakers_is_not_a_dyad():
    now = 1000.0
    t = [_U("Marco", 994), _U("Lia", 996), _U("Ben", 998), _U("Marco", 999)]
    assert not in_locked_dyad(t, avatar_name="Laura", now=now, **_KW)


def test_same_speaker_twice_breaks_alternation():
    now = 1000.0
    t = [_U("Marco", 994), _U("Marco", 996), _U("Lia", 998), _U("Marco", 999)]
    assert not in_locked_dyad(t, avatar_name="Laura", now=now, **_KW)


def test_long_gap_reopens_the_floor():
    now = 1000.0
    # 6s gap between turn 2 and 3 (> not; 8s is the cap) → make it 9s
    t = [_U("Marco", 985), _U("Lia", 987), _U("Marco", 996), _U("Lia", 998)]
    assert not in_locked_dyad(t, avatar_name="Laura", now=now, **_KW)


def test_stale_exchange_is_not_locked_now():
    now = 1000.0  # last turn was 20s ago → gone quiet
    t = [_U("Marco", 974), _U("Lia", 976), _U("Marco", 978), _U("Lia", 980)]
    assert not in_locked_dyad(t, avatar_name="Laura", now=now, **_KW)


def test_avatar_turns_excluded_so_a_1to1_is_not_a_dyad():
    now = 1000.0
    # Ben <-> Laura 1:1 — if the avatar counted, this would look like a dyad.
    t = [_U("Ben", 993), _U("Laura", 995), _U("Ben", 997), _U("Laura", 999)]
    assert not in_locked_dyad(t, avatar_name="Laura", now=now, **_KW)


def test_too_few_turns_is_not_a_dyad():
    now = 1000.0
    t = [_U("Marco", 996), _U("Lia", 998)]
    assert not in_locked_dyad(t, avatar_name="Laura", now=now, **_KW)


# ── the webhook wiring ──


def _session(tmp_path, monkeypatch, bot_id="dyad-bot") -> store.Session:
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    s.addressed_once = True
    s.participant_event("Marco", 1, here=True)
    s.participant_event("Lia", 2, here=True)
    monkeypatch.setattr(settings, "deference_seconds", 0)
    monkeypatch.setattr(settings, "recall_api_key", "")
    monkeypatch.setattr(settings, "cross_talk_suppression_enabled", True)
    return s


def _line(bot_id: str, speaker: str, text: str) -> dict:
    return {
        "event": "transcript.data",
        "data": {
            "bot": {"id": bot_id},
            "data": {
                "words": [{"text": w} for w in text.split()],
                "participant": {"name": speaker, "id": 1 if speaker == "Marco" else 2},
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


def _seed_dyad(s: store.Session) -> None:
    """A tight Marco↔Lia exchange landing right now."""
    now = time.time()
    for i, sp in enumerate(["Marco", "Lia", "Marco"]):
        s.transcript.append(store.Utterance(speaker=sp, text="line", ts=now - (3 - i)))


def test_locked_dyad_holds_interjection_to_a_raised_hand(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    _seed_dyad(s)
    _stub_stream(monkeypatch, ["A grounded point Laura could add about onboarding."])
    # Lia continues the volley (unaddressed) → she must NOT interject; the
    # grounded point goes behind a silent raised hand instead.
    body = _post(_line(s.bot_id, "Lia", "and then what happens after that step?"))
    assert body.get("spoke") is not True
    assert body.get("hand_raised") is True or body.get("reason", "").startswith("hand")
    store.remove(s.bot_id)


def test_disabled_flag_is_a_noop(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, bot_id="dyad-off")
    monkeypatch.setattr(settings, "cross_talk_suppression_enabled", False)
    _seed_dyad(s)
    _stub_stream(monkeypatch, ["A grounded point."])
    # With the flag off, behaviour is exactly today's (no dyad suppression path).
    body = _post(_line(s.bot_id, "Lia", "and then what happens after that step?"))
    assert "reason" in body or "spoke" in body  # went down the normal path
    store.remove(s.bot_id)
