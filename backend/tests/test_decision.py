"""Pure-logic tests for the when-to-speak gate. No API keys needed.

Run:  pytest backend/tests -q   (pip install pytest first)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.avatars import Avatar  # noqa: E402
from app.decision import detect_wake, fuzzy_name_match, passes_confidence  # noqa: E402


def _avatar(**over) -> Avatar:
    base = dict(
        id="laura",
        name="Laura",
        role="AI Process Expert",
        wake_words=["laura"],
        persona_prompt="",
        anam_avatar_id="r1",
        elevenlabs_voice_id="v1",
        min_confidence=0.55,
        speak_cooldown_seconds=8.0,
        dir=Path("."),
    )
    base.update(over)
    return Avatar(**base)


def test_wake_triggers_and_strips_name():
    called, q = detect_wake(_avatar(), "Laura, what are we missing?")
    assert called is True
    assert q.lower() == "what are we missing"


def test_no_wake_word_stays_silent():
    called, q = detect_wake(_avatar(), "What is the onboarding process?")
    assert called is False
    assert q == ""


def test_wake_word_must_be_whole_token():
    # "lauralike" should NOT trigger the "laura" wake word.
    called, _ = detect_wake(_avatar(), "this is lauralike behaviour")
    assert called is False


def test_custom_wake_words():
    called, _ = detect_wake(_avatar(wake_words=["marcus", "it expert"]), "hey Marcus?")
    assert called is True


def test_confidence_gate():
    a = _avatar(min_confidence=0.6)
    assert passes_confidence(a, {"sufficient_context": True, "confidence": 0.7})
    assert not passes_confidence(a, {"sufficient_context": True, "confidence": 0.5})
    assert not passes_confidence(a, {"sufficient_context": False, "confidence": 0.9})


# ── stop command ("Laura, stop / aspetta") ──
from app.decision import detect_stop_command  # noqa: E402

STOP_ASKS = [
    "stop",
    "wait",
    "hold on",
    "one sec",
    "okay stop please",
    "never mind",
    "stop talking",
    "that's enough",
    "aspetta",
    "fermati",
    "un attimo per favore",
    "basta così",
    "zitta",
    "lascia stare",
]

NOT_STOP_ASKS = [
    "stop the deploy",
    "when do we stop the meter",
    "wait for the client to confirm",
    "can you pause the recording",
    "aspetta il cliente prima di mandare la mail",
    "basta parlare del budget, passiamo oltre",
    "what happens if we stop paying",
]


def test_stop_commands_detected():
    for ask in STOP_ASKS:
        assert detect_stop_command(ask), f"should stop: {ask!r}"


def test_stop_not_triggered_by_normal_talk():
    for ask in NOT_STOP_ASKS:
        assert not detect_stop_command(ask), f"must NOT stop: {ask!r}"


def test_italian_closing_detected():
    from app.decision import detect_closing
    for line in (
        "per riassumere, direi che ci siamo",
        "prima di chiudere, un'ultima cosa",
        "abbiamo finito per oggi",
        "qualcos'altro da discutere?",
        "ci aggiorniamo la prossima settimana",
    ):
        assert detect_closing(line), line


# ── action-capture continuation guard (main.py's 4s same-speaker window) ──
# The window exists for real ASR splits; a follow-up that opens like a NEW
# sentence (acknowledgement marker / wrap-up line) must not read as one.


def test_capture_continuation_true_for_real_asr_splits():
    from app.decision import is_capture_continuation
    for text in (
        "to the whole team by Friday",
        "entro venerdì a Marco",
        "and cc the finance team",
        "il recap della riunione di oggi",
    ):
        assert is_capture_continuation(text), text


def test_capture_continuation_false_for_new_sentences():
    from app.decision import is_capture_continuation
    for text in (
        "perfetto, direi che abbiamo finito il test",  # live repro 2026-07-10
        "ok so let's move on to the next topic",
        "great, thanks everyone",
        "grazie mille",
        "va bene, passiamo oltre",
        "anything else before we wrap up?",  # closing without an opener marker
    ):
        assert not is_capture_continuation(text), text


def test_capture_continuation_blank_is_not_a_continuation():
    from app.decision import is_capture_continuation
    assert not is_capture_continuation("")
    assert not is_capture_continuation("   ")


def test_italian_reported_speech_does_not_wake():
    from app.avatars import Avatar
    from app.decision import detect_wake
    from pathlib import Path
    a = Avatar(id="laura", name="Laura", role="x", wake_words=["laura"],
               persona_prompt="", anam_avatar_id="", elevenlabs_voice_id="",
               min_confidence=0.5, speak_cooldown_seconds=8.0, dir=Path("."))
    for line in (
        "come ha detto Laura, il DPA manca",
        "secondo Laura dovremmo aspettare",
        "Laura ha detto che il deadline è venerdì",
        "Laura diceva una cosa simile",
    ):
        called, _ = detect_wake(a, line)
        assert not called, line
    # vocative still wakes
    called, q = detect_wake(a, "Laura, cosa ne pensi?")
    assert called and q


# ── phonetic-initial fuzzy match: STT mangles the soft-C name "Cedric" ──
# (spoken "Cedric" is transcribed "Sedric"/"Kedric"/"Zedric" — the old literal
# first-letter gate hard-rejected all of them, silently costing Cedric answers).


def test_fuzzy_matches_sibilant_initial_corruptions():
    for token in ("sedric", "kedric", "zedric"):
        assert fuzzy_name_match(token, "cedric"), token


def test_fuzzy_still_rejects_unrelated_names():
    # the relaxed initial must NOT open the gate to genuinely different names
    assert not fuzzy_name_match("frederick", "cedric")
    assert not fuzzy_name_match("patrick", "cedric")
    # non-sibilant initials are unaffected — "clara"/"sara" never wake "laura"
    assert not fuzzy_name_match("clara", "laura")
    assert not fuzzy_name_match("sara", "laura")
    # existing Laura behaviour preserved (distance-1 + skeleton rules)
    assert fuzzy_name_match("lora", "laura")
    assert not fuzzy_name_match("libra", "laura")


def test_detect_wake_cedric_asr_spellings_via_fuzzy():
    # bare wake word (NO aliases) — proves the fuzzy path alone now resolves the
    # soft-C corruptions, so this generalises beyond the exact-match aliases.
    cedric = _avatar(id="cedric", name="Cedric", wake_words=["cedric"])
    for utt in ("Sedric, what's the plan?", "Kedric, can you check?", "Zedric, hi"):
        called, _ = detect_wake(cedric, utt)
        assert called, utt
    # a genuinely different name must not wake him
    assert detect_wake(cedric, "Frederick, can you take this?")[0] is False


# ── high-confidence interjection escape (decision.interjection_floor_open /
#    decision.should_interject) — DEMO-READY-ROADMAP §5 item 10 ──


def test_floor_open_when_line_finished_and_no_partial():
    from app.decision import interjection_floor_open
    # A finished-sounding line (completeness high) with no human partial in
    # flight (the last one was long ago) → the floor is open to interject.
    assert interjection_floor_open(
        turn_completeness=0.9, since_human_partial=10.0, active_partial_seconds=0.6
    )
    # No completeness estimate at all still opens if nobody is talking now.
    assert interjection_floor_open(
        turn_completeness=None, since_human_partial=10.0, active_partial_seconds=0.6
    )


def test_floor_closed_when_speaker_mid_thought():
    from app.decision import interjection_floor_open
    # The line sounds mid-thought (low completeness) — hold the floor, raise the
    # hand instead of interjecting.
    assert not interjection_floor_open(
        turn_completeness=0.1, since_human_partial=10.0, active_partial_seconds=0.6
    )


def test_floor_closed_when_human_partial_in_flight():
    from app.decision import interjection_floor_open
    # A human partial landed 0.2s ago (< active_partial_seconds) — someone is
    # audibly talking right now, so never interject even on a finished line.
    assert not interjection_floor_open(
        turn_completeness=0.95, since_human_partial=0.2, active_partial_seconds=0.6
    )


def test_should_interject_only_on_high_confidence_open_floor():
    from app.decision import should_interject
    # High confidence + open floor → interject.
    assert should_interject(
        enabled=True, confidence=0.62, min_confidence=0.5, floor_open=True
    )
    # Below the bar → keep the safe raised hand.
    assert not should_interject(
        enabled=True, confidence=0.44, min_confidence=0.5, floor_open=True
    )
    # High confidence but the floor is busy → raise the hand.
    assert not should_interject(
        enabled=True, confidence=0.9, min_confidence=0.5, floor_open=False
    )
    # Feature disabled → never interject, whatever the confidence.
    assert not should_interject(
        enabled=False, confidence=0.99, min_confidence=0.5, floor_open=True
    )
    # A bar above 1.0 is the "disable" escape hatch — nothing clears it.
    assert not should_interject(
        enabled=True, confidence=1.0, min_confidence=1.5, floor_open=True
    )


# ── closing fallback (decision.closing_fallback_fires) — §5 item 12 ──


def test_closing_fallback_fires_on_idle_after_long_meeting():
    from app.decision import closing_fallback_fires
    now = 10_000.0
    # Meeting started 300s ago; last substantive line was 30s ago (idle) → fires.
    assert closing_fallback_fires(
        enabled=True, now=now, meeting_start=now - 300,
        last_line_at=now - 30, idle_seconds=25.0, min_meeting_seconds=180.0,
    )


def test_closing_fallback_silent_in_short_call():
    from app.decision import closing_fallback_fires
    now = 10_000.0
    # A long lull but the whole meeting is only 60s old → duration gate blocks it.
    assert not closing_fallback_fires(
        enabled=True, now=now, meeting_start=now - 60,
        last_line_at=now - 40, idle_seconds=25.0, min_meeting_seconds=180.0,
    )


def test_closing_fallback_silent_in_active_call():
    from app.decision import closing_fallback_fires
    now = 10_000.0
    # Long meeting, but the room is actively talking (last line 5s ago) → idle
    # gate blocks it.
    assert not closing_fallback_fires(
        enabled=True, now=now, meeting_start=now - 600,
        last_line_at=now - 5, idle_seconds=25.0, min_meeting_seconds=180.0,
    )


def test_closing_fallback_disabled_and_no_prior_line():
    from app.decision import closing_fallback_fires
    now = 10_000.0
    # Disabled → never fires even when both gates would pass.
    assert not closing_fallback_fires(
        enabled=False, now=now, meeting_start=now - 600,
        last_line_at=now - 40, idle_seconds=25.0, min_meeting_seconds=180.0,
    )
    # No previous line seen (last_line_at <= 0) → never fires.
    assert not closing_fallback_fires(
        enabled=True, now=now, meeting_start=now - 600,
        last_line_at=0.0, idle_seconds=25.0, min_meeting_seconds=180.0,
    )
    # A non-positive threshold disables the fallback too.
    assert not closing_fallback_fires(
        enabled=True, now=now, meeting_start=now - 600,
        last_line_at=now - 40, idle_seconds=0.0, min_meeting_seconds=180.0,
    )


def test_deference_default_trimmed_reduces_dead_air():
    """Item 11: the base deference wait was trimmed 1.8 → 1.2 to cut the dead air
    before an unprompted line. It flows through the adaptive sizer as the base,
    so an unprompted STATEMENT now waits 1.2s (was 1.8s), a real latency cut with
    the yield check unchanged."""
    from app.config import settings
    from app.decision import adaptive_deference_seconds
    assert settings.deference_seconds == 1.2
    wait = adaptive_deference_seconds(
        settings.deference_seconds,
        enabled=True,
        lo=settings.deference_min_seconds,
        hi=settings.deference_max_seconds,
        since_partial=10.0,
        active_partial_seconds=settings.deference_active_partial_seconds,
        n_humans=3,
        is_question=False,
        turn_completeness=0.9,  # clearly finished
    )
    assert wait == 1.2  # the trimmed base, < the old 1.8s dead-air wait
