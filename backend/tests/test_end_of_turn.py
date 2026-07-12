"""End-of-turn completeness + how it sizes the deference wait.

The estimator only ever SIZES a wait (never decides speech), so the tests pin
the calibration anchors — clearly-done >= 0.8, clearly-mid-thought <= 0.3 —
and the sizer's precedence: mid-thought outranks every other signal.
"""
from app import end_of_turn
from app.decision import adaptive_deference_seconds


# ── the classifier ──

def test_full_questions_are_clearly_done():
    assert end_of_turn.completeness("What's the deadline for the migration?") >= 0.8
    assert end_of_turn.completeness("Qual è la scadenza per la migrazione?") >= 0.8


def test_question_mark_outranks_trailing_incomplete_word():
    assert end_of_turn.completeness("What did Laura mean by that?") >= 0.8
    assert end_of_turn.completeness("Who is this for?") >= 0.8
    assert end_of_turn.completeness("È questo che volevi?") >= 0.8


def test_trailing_connectives_are_mid_thought_bilingual():
    assert end_of_turn.completeness("I wanted to ask about the") <= 0.3
    assert end_of_turn.completeness("Volevo chiederti della") <= 0.3
    assert end_of_turn.completeness("We could do that and") <= 0.3
    assert end_of_turn.completeness("Dobbiamo parlare con il") <= 0.3


def test_fillers_and_trail_offs_hold_the_floor():
    assert end_of_turn.completeness("so the plan would be, uhm") <= 0.3
    assert end_of_turn.completeness("allora, vediamo") <= 0.3
    assert end_of_turn.completeness("and then we...") <= 0.3


def test_punctuated_statements_read_done():
    assert end_of_turn.completeness("The rollout finished yesterday.") >= 0.8


def test_trailing_emoji_preserves_completed_punctuation():
    assert end_of_turn.completeness("Great job! 🎉") >= 0.8
    assert end_of_turn.completeness("Complimenti! 🚀🚀") >= 0.8
    assert end_of_turn.completeness("Is this ready? 🤔") >= 0.8
    assert end_of_turn.completeness("Looks good! ❤️") >= 0.8
    assert end_of_turn.completeness("All done! 👍🏽") >= 0.8


def test_trailing_emoji_does_not_hide_ellipsis_or_invent_content():
    assert end_of_turn.completeness("and then... 😬") <= 0.3
    assert end_of_turn.completeness("🎉") == 0.0


def test_short_period_anchor_remains_uncertain():
    assert end_of_turn.completeness("So. ✅") < 0.8


def test_short_fragments_are_uncertain_not_done():
    # 1-2 word unpunctuated fragments must never read "clearly done".
    assert end_of_turn.completeness("the deadline") < 0.8
    assert end_of_turn.completeness("ok") < 0.8
    assert end_of_turn.completeness("") == 0.0


def test_asr_unpunctuated_clause_stays_middling():
    # Live ASR often drops punctuation: don't overcommit either way.
    c = end_of_turn.completeness("what do you think about moving the deadline")
    assert 0.3 < c < 0.8


# ── the sizer (precedence) ──

_KW = dict(enabled=True, lo=1.0, hi=2.6, since_partial=99.0,
           active_partial_seconds=0.6)


def test_mid_thought_outranks_everything():
    # Even in a single-human room (the snappiest branch), a trailing
    # conjunction means the speaker holds the floor: wait the max.
    w = adaptive_deference_seconds(
        1.8, **_KW, n_humans=1, is_question=False,
        turn_completeness=end_of_turn.completeness("volevo chiederti della"),
    )
    assert w == 2.6


def test_clearly_done_question_answers_snappily():
    w = adaptive_deference_seconds(
        1.8, **_KW, n_humans=3, is_question=True,
        turn_completeness=end_of_turn.completeness("Qual è la scadenza?"),
    )
    assert w == 1.0


def test_uncertain_question_keeps_the_82_compromise():
    # No completeness verdict either way -> the original mid-point behaviour.
    w = adaptive_deference_seconds(
        1.8, **_KW, n_humans=3, is_question=True, turn_completeness=0.5,
    )
    assert w == (1.8 + 1.0) / 2.0


def test_disabled_stays_a_strict_noop():
    w = adaptive_deference_seconds(
        1.8, enabled=False, lo=1.0, hi=2.6, since_partial=99.0,
        active_partial_seconds=0.6, n_humans=1, is_question=True,
        turn_completeness=0.1,
    )
    assert w == 1.8


def test_completeness_none_reproduces_pr82_behaviour():
    # Callers without a completeness signal get exactly the #82 sizing.
    w = adaptive_deference_seconds(
        1.8, **_KW, n_humans=1, is_question=False, turn_completeness=None,
    )
    assert w == 1.0
