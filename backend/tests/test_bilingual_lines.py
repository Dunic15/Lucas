"""Bilingual conversational furniture: Italian questions trigger the same
features (search, think, announces) as English ones, with Italian lines."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402
from app.brain import engine as brain  # noqa: E402
from app.config import settings  # noqa: E402


def test_sounds_italian():
    assert brain.sounds_italian("Laura, cosa dicono le ultime notizie di oggi?")
    assert brain.sounds_italian("come funziona questa cosa, puoi spiegare?")
    assert not brain.sounds_italian("what's the latest news today?")
    assert not brain.sounds_italian("ok")


def test_italian_search_intent(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "live_search_enabled", True)
    for q in (
        "cerca le ultime notizie su OpenAI",
        "quanto costa un abbonamento ChatGPT oggi?",
        "che tempo fa a Milano?",
        "chi ha vinto la partita ieri?",
    ):
        assert brain.wants_web_search(q), q
    # Italian process talk must NOT trigger search
    assert not brain.wants_web_search("chi si occupa della sicurezza?")


def test_personal_reads_and_asks_never_route_to_search(monkeypatch):
    """Live 2026-07-22: freshness words ("this week", "right now") and the SFF
    branch hijacked calendar/Asana reads and a dictated email address onto the
    web-search path, whose model then declared "I don't have access to your
    calendar/Asana". Personal-context reads answer from the grounded briefs;
    imperative asks belong to the capture seam."""
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    for q in (
        "what's on my calendar this week?",
        "cosa ho in calendario questa settimana?",
        "what's open in Asana right now, Petra?",
        "can you check my meetings today?",
        "send an email to duccio at sff studio dot com with a recap",
        "send an email to duccio@sffstudio.com today",
        "schedule a follow-up for today at 3pm",
    ):
        assert not brain.wants_web_search(q), q
    # …while genuine fresh-info queries keep searching (incl. the SFF branch).
    for q in (
        "what's the weather in Milan right now?",
        "latest news about OpenAI",
        "tell me about the SFF portfolio",
    ):
        assert brain.wants_web_search(q), q


def test_italian_complex_intent(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    for q in (
        "analizza i pro e contro di questa scelta",
        "confronta le due opzioni nel dettaglio",
        "dovremmo assumere un altro sviluppatore?",
    ):
        assert brain.wants_deep_thought(q), q


def test_line_for_picks_language():
    it = main._line_for(
        "Laura, cosa ne pensi di questa proposta per il cliente?",
        main._ACK_LINES,
        main._ACK_LINES_IT,
    )
    en = main._line_for(
        "what do you think about this proposal?",
        main._ACK_LINES,
        main._ACK_LINES_IT,
    )
    assert it in main._ACK_LINES_IT
    assert en in main._ACK_LINES


def test_search_announce_language():
    assert set(brain.SEARCH_ANNOUNCE_LINES) == set(
        brain._SEARCH_ANNOUNCE_EN + brain._SEARCH_ANNOUNCE_IT
    )
    assert len(brain._SEARCH_ANNOUNCE_EN) >= 5  # enough variants to dodge the
    assert len(brain._SEARCH_ANNOUNCE_IT) >= 5  # 120s repeat-suppress window
