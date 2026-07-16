"""The relay-echo bug: Gemini ears re-transcribes the avatar's OWN voice from
the meeting's mixed audio and posts it as a 'human' turn — she answers herself.

_is_echo's original exact-substring test only caught Recall's near-verbatim
echo. A second ASR (Gemini) drifts in wording/punctuation and aggregates one
turn across several spoken lines; the token-coverage extension must catch
that, while a real human turn — even one quoting her — still gets through.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from app.main import _is_echo, _norm_line


def _session(*spoken_lines: str) -> SimpleNamespace:
    now = time.time()
    return SimpleNamespace(
        _recent_lines={_norm_line(s): now for s in spoken_lines}
    )


SPOKEN_1 = "Our pricing starts at ninety nine euros per month for the solo plan."
SPOKEN_2 = "That includes three hundred avatar minutes and unlimited artifacts."


def test_verbatim_echo_still_caught():
    s = _session(SPOKEN_1)
    assert _is_echo(s, "pricing starts at ninety nine euros per month") is True


def test_cross_asr_reworded_echo_caught():
    """Gemini's transcription: same words, different segmentation/punctuation —
    the old substring test missed this exact case."""
    s = _session(SPOKEN_1, SPOKEN_2)
    gemini_turn = (
        "our pricing starts at ninety nine euros per month, "
        "for the solo plan that includes three hundred avatar minutes"
    )
    assert _is_echo(s, gemini_turn) is True


def test_turn_spanning_multiple_spoken_lines_caught():
    s = _session(SPOKEN_1, SPOKEN_2)
    aggregated = f"{SPOKEN_1} {SPOKEN_2}"
    assert _is_echo(s, aggregated) is True


def test_real_human_turn_not_suppressed():
    s = _session(SPOKEN_1)
    human = "wait can you explain why the enterprise tier would cost more than that"
    assert _is_echo(s, human) is False


def test_human_partially_quoting_her_not_suppressed():
    """A human referencing her words adds their own — coverage stays under
    the bar and the turn must be answered."""
    s = _session(SPOKEN_1)
    human = (
        "you said ninety nine euros per month but our budget committee "
        "explicitly capped subscriptions at fifty"
    )
    assert _is_echo(s, human) is False


def test_short_lines_left_to_other_gates():
    s = _session(SPOKEN_1)
    assert _is_echo(s, "ninety nine") is False


def test_expired_window_not_echo():
    s = SimpleNamespace(
        _recent_lines={_norm_line(SPOKEN_1): time.time() - 120.0}
    )
    assert _is_echo(s, SPOKEN_1) is False
