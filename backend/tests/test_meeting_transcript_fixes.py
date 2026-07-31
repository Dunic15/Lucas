"""Regressions from the 2026-07-31 live test meeting (Cedric transcript):
wake-word misses ("Hedrik"), the post-answer wedge (runaway speaking window +
echo over-matching), spoken mid-sentence truncation, dishonest capability
claims, stripped attendee emails, and the missing awaiting-reply window that
forced re-addressing the avatar to answer its own question. Key-free."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import avatars, main, store  # noqa: E402
from app.config import settings  # noqa: E402
from app.meeting.decision import detect_wake, fuzzy_name_match  # noqa: E402


# ── harness (same shape as test_address_only_group) ──

def _session(tmp_path, monkeypatch, bot_id="fix-bot", humans=("Ben", "Marco")):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "store.sqlite3")
    store._init_db()
    s = store.create(bot_id, "https://meet.google.com/abc-defg-hij", "laura")
    s.memory_brief = ""
    s.addressed_once = True
    for i, name in enumerate(humans, start=1):
        s.participant_event(name, i, here=True)
    monkeypatch.setattr(settings, "deference_seconds", 0)
    monkeypatch.setattr(settings, "recall_api_key", "")
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


# ── fuzzy wake: initial-consonant corruption (transcript: "Hedrik") ──

def test_fuzzy_wake_crosses_initial_gate_at_distance_one():
    assert fuzzy_name_match("hedrik", "sedrik") is True
    assert fuzzy_name_match("hedrick", "sedrick") is True
    # distance 2 with a different initial stays blocked
    assert fuzzy_name_match("clara", "laura") is False
    # short names (<6) never cross the initial gate ("maura" ≠ Laura)
    assert fuzzy_name_match("maura", "laura") is False
    # far corruptions stay unmatched
    assert fuzzy_name_match("sedger", "sedric") is False


def test_hedrik_wakes_cedric():
    cedric = avatars.load("cedric")
    called, _ = detect_wake(cedric, "Hedrik, did you get that?")
    assert called is True


# ── echo filter: short verbatim reply to her own question is NOT echo ──

def _echo_session(*spoken):
    now = time.time()
    return SimpleNamespace(
        _recent_lines={main._norm_line(s): now for s in spoken}
    )


LONG_SPOKEN = (
    "Got it, I am queuing up two meetings. The first is to test the "
    "capabilities, and the second is to get a client by next Monday. "
    "What time would you like for each one of them?"
)


def test_short_reply_inside_long_answer_not_swallowed():
    s = _echo_session(LONG_SPOKEN)
    # 4 words, tiny fraction of the long spoken line — a human reply, not echo
    assert main._is_echo(s, "get a client by") is False


def test_substantial_verbatim_chunk_still_echo():
    s = _echo_session(LONG_SPOKEN)
    heard = "queuing up two meetings the first is to test the capabilities"
    assert main._is_echo(s, heard) is True  # ≥8 words


def test_short_line_matching_short_spoken_still_echo():
    s = _echo_session("The deadline is Friday at noon.")
    assert main._is_echo(s, "deadline is friday at noon") is True  # ≥50% ratio


# ── speaking window: hard cap + awaiting-reply arming ──

def test_speaking_until_capped_and_question_arms_reply_window(
    tmp_path, monkeypatch
):
    s = _session(tmp_path, monkeypatch, bot_id="fix-cap")
    long_text = "word " * 400  # ~154s at 2.6 wps — must not push the window that far

    async def run():
        for _ in range(3):
            await main._make_avatar_speak(s, long_text.strip() + ".", force=True)

    asyncio.run(run())
    assert s.speaking_until <= time.time() + 45.5

    assert s.awaiting_reply_until == 0.0
    asyncio.run(main._make_avatar_speak(s, "What time works for you?", force=True))
    assert s.awaiting_reply_until > time.time()
    store.remove(s.bot_id)


# ── awaiting-reply window: 1:1 only; group rooms require the name ALWAYS ──

def test_group_room_requires_name_even_to_answer_her_question(tmp_path, monkeypatch):
    """Owner rule (2026-07-31): the awaiting-reply window never bypasses
    address-only group mode — with several humans present, an unaddressed
    reply to HER question stays silent and the window is NOT consumed."""
    s = _session(tmp_path, monkeypatch, bot_id="fix-reply-grp")
    _stub_stream(monkeypatch, ["I should not speak."])
    spoken = _capture_speech(monkeypatch)

    armed_until = time.time() + settings.awaiting_reply_seconds
    s.awaiting_reply_until = armed_until
    body = _post(_line(s.bot_id, "Ben", "the client meeting is Monday at five PM"))
    assert body.get("spoke") is False
    assert body.get("reason") == "not addressed (group)"
    assert not spoken
    assert s.awaiting_reply_until == armed_until  # not consumed by the gate
    store.remove(s.bot_id)


def test_one_to_one_reply_to_her_question_answers_despite_cooldown(
    tmp_path, monkeypatch
):
    """1:1: she just asked, so the bare non-question reply ("Monday at five
    PM") answers even inside the cooldown — then the window is consumed."""
    s = _session(tmp_path, monkeypatch, bot_id="fix-reply-1to1", humans=("Ben",))
    monkeypatch.setattr(settings, "ack_enabled", False)
    _stub_stream(monkeypatch, ["Noted — five PM it is."])
    spoken = _capture_speech(monkeypatch)

    s.mark_spoke()  # she JUST spoke (the question) — cooldown is active
    s.awaiting_reply_until = time.time() + settings.awaiting_reply_seconds
    body = _post(_line(s.bot_id, "Ben", "the client meeting is Monday at five PM"))
    assert body.get("spoke") is True
    assert spoken
    assert s.awaiting_reply_until == 0.0  # consumed on use
    store.remove(s.bot_id)


def test_one_to_one_reply_window_disabled_by_setting(tmp_path, monkeypatch):
    """With the window disabled, the same bare reply inside the cooldown is
    silent — pinning that the window (not something else) made it answer."""
    s = _session(tmp_path, monkeypatch, bot_id="fix-reply-off", humans=("Ben",))
    monkeypatch.setattr(settings, "awaiting_reply_seconds", 0.0)
    monkeypatch.setattr(settings, "ack_enabled", False)
    _stub_stream(monkeypatch, ["I should not speak."])
    _capture_speech(monkeypatch)

    s.mark_spoke()
    s.awaiting_reply_until = time.time() + 20
    body = _post(_line(s.bot_id, "Ben", "Monday at five PM works"))
    assert body.get("spoke") is False
    assert body.get("reason") == "cooldown"
    store.remove(s.bot_id)


# ── truncation: an unterminated tail is trimmed, never voiced ──

def test_stream_tail_fragment_dropped(monkeypatch):
    from app.brain import engine, llm

    def fake_stream(system, user, max_tokens, model=None, provider=None):
        yield "Here is the first full sentence. And then a dangling fragm"

    monkeypatch.setattr(llm, "stream_complete", fake_stream)
    monkeypatch.setattr(engine, "_is_stub", lambda: False)  # keyless env → stub path otherwise
    laura = avatars.load("laura")
    chunks = list(engine.answer_question_stream(laura, "quick summary please?"))
    assert chunks
    assert all(c.strip()[-1] in ".!?…" for c in chunks)
    assert not any("dangling" in c for c in chunks)


def test_stream_short_unpunctuated_answer_still_spoken(monkeypatch):
    from app.brain import engine, llm

    def fake_stream(system, user, max_tokens, model=None, provider=None):
        yield "Sure"

    monkeypatch.setattr(llm, "stream_complete", fake_stream)
    monkeypatch.setattr(engine, "_is_stub", lambda: False)
    laura = avatars.load("laura")
    chunks = list(engine.answer_question_stream(laura, "can you help?"))
    assert chunks == ["Sure"]


# ── calendar brief: attendee emails survive ──

def test_event_line_keeps_attendee_emails():
    from app.integrations import google_client

    line = google_client._event_line({
        "summary": "Rooftop evening",
        "start": {"dateTime": "2026-08-26T18:00:00+02:00"},
        "end": {"dateTime": "2026-08-26T21:00:00+02:00"},
        "attendees": [
            {"displayName": "Duccio", "email": "duccio@sffstudio.com"},
            {"email": "ben@sffstudio.com"},
            {"self": True, "email": "me@sffstudio.com"},
        ],
    })
    assert "Duccio <duccio@sffstudio.com>" in line
    assert "ben@sffstudio.com" in line
    assert "me@sffstudio.com" not in line  # self is never a guest


# ── tool registry: claims follow the ACTUAL fetch outcome ──

def test_brief_claims_follow_fetch_outcome():
    from app.brain import tool_registry

    reg = {
        "native": [], "cedric": {},
        "knowledge": {
            "docs": True, "drive_folder_configured": True,
            "drive_folder": False, "calendar": False,
        },
    }
    text = tool_registry.brief(reg)
    assert "did not load" in text and "don't have the Drive docs" in text

    reg["knowledge"]["drive_folder"] = True
    reg["knowledge"]["calendar"] = True
    text = tool_registry.brief(reg)
    assert "Drive folder brief (in your context)" in text
    assert "upcoming calendar" in text
    assert "did not load" not in text


# ── persona parity: every product avatar gets the same conversation rules ──

def test_product_avatar_personas_are_honest_about_context():
    """Owner rule: conversation behaviour is IDENTICAL across product avatars
    (only knowledge, integrations, and the skills they describe differ). The
    transcript bug was a persona ASSERTING context it might not hold, so both
    personas must defer to what's actually in context and must not promise a
    mid-meeting lookup the live path can't do (there are no callable tools on
    the transcript path — only what the session-start brief put in the
    prompt)."""
    for avatar_id in ("laura", "cedric"):
        p = avatars.load(avatar_id).persona_prompt
        assert "ACTUALLY" in p, avatar_id          # answer from real context
        assert "never deny something" in p, avatar_id
        assert "queue_action" in p, avatar_id      # capture, don't claim done
        assert "NEVER claim it's already done" in p, avatar_id
        # No promise to go searching mid-meeting for older material.
        assert "search the team's meeting memory" not in p, avatar_id


# ── ASR keyterms ──

def test_keyterms_from_name_and_setting(monkeypatch):
    from app.integrations import recall_client

    monkeypatch.setattr(settings, "asr_keyterms", "Lauratar, cedric, , Lauratar")
    assert recall_client._keyterms("Cedric") == ["Cedric", "Lauratar"]

    monkeypatch.setattr(settings, "asr_keyterms", "")
    assert recall_client._keyterms("Laura") == ["Laura"]
