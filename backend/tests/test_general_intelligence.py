"""Live path: general-assistant behavior — search routing + conditional grounding."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import brain  # noqa: E402
from app.avatars import Avatar  # noqa: E402
from app.config import settings  # noqa: E402


def _avatar() -> Avatar:
    return Avatar(
        id="laura",
        name="Laura",
        role="AI Assistant & SFF Expert",
        wake_words=["laura"],
        persona_prompt="",
        anam_avatar_id="r1",
        elevenlabs_voice_id="v1",
        min_confidence=0.55,
        speak_cooldown_seconds=8.0,
        dir=Path("."),
    )


def _force_groq(monkeypatch) -> None:
    monkeypatch.setattr(settings, "brain_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", True)


def test_search_questions_route_to_compound(monkeypatch):
    monkeypatch.setattr(settings, "brain_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", True)
    assert brain._live_model("search the internet for the latest AI news") == settings.live_search_model
    assert brain._live_model("what's the weather today in Milan?") == settings.live_search_model
    assert brain._live_model("who won the match yesterday?") == settings.live_search_model


def test_normal_questions_keep_fast_model(monkeypatch):
    monkeypatch.setattr(settings, "brain_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", True)
    assert brain._live_model("what are we missing before go-live?") == settings.brain_model_fast
    assert brain._live_model("explain what a DPA is") == settings.brain_model_fast


def test_search_routes_regardless_of_live_provider(monkeypatch):
    """Search runs on Groq compound even when the live brain is Claude."""
    monkeypatch.setattr(settings, "brain_provider", "anthropic")
    monkeypatch.setattr(settings, "groq_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", True)
    assert brain._live_model("latest news please") == settings.live_search_model


def test_search_routing_respects_flag_and_key(monkeypatch):
    monkeypatch.setattr(settings, "brain_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "")
    assert brain._live_model("latest news please") == settings.brain_model_fast
    monkeypatch.setattr(settings, "groq_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", False)
    assert brain._live_model("latest news please") == settings.brain_model_fast


def test_prompt_is_assistant_first():
    p = brain.ANSWER_STREAM_SYSTEM
    assert "capable general assistant FIRST" in p
    assert "Never refuse just because it isn't in the documents" in p
    assert "SKIP" in p  # silence gate preserved


# ── web search on the interactive /live/act path (phase 2) ──
def test_web_search_answer_empty_on_refusal_or_blank(monkeypatch):
    monkeypatch.setattr(brain.llm, "complete", lambda *a, **k: "I'm not able to browse the web right now.")
    assert brain._web_search_answer("latest news") == ""
    monkeypatch.setattr(brain.llm, "complete", lambda *a, **k: "")
    assert brain._web_search_answer("latest news") == ""
    monkeypatch.setattr(brain.llm, "complete", lambda *a, **k: "Milan is sunny, 24°C — from a quick search.")
    assert "quick search" in brain._web_search_answer("weather Milan today")


def test_interactive_path_uses_web_search_for_current_info(monkeypatch):
    _force_groq(monkeypatch)
    monkeypatch.setattr(
        brain, "_web_search_answer", lambda q, convo="": "Milan is sunny — from a quick search."
    )
    r = brain.answer_with_tools(_avatar(), "what's the weather in Milan today?")
    assert "quick search" in r["answer"]
    assert r["tools_used"] and r["tools_used"][0]["tool"] == "web_search"


def test_interactive_path_falls_back_when_search_flakes(monkeypatch):
    _force_groq(monkeypatch)
    monkeypatch.setattr(brain, "_web_search_answer", lambda q, convo="": "")  # search returned nothing
    monkeypatch.setattr(brain, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(brain.llm, "complete_with_tools", lambda *a, **k: ("Here's my take.", []))
    r = brain.answer_with_tools(_avatar(), "what's the latest news on X?")
    assert r["answer"] == "Here's my take."
    assert not r["tools_used"]


def test_interactive_path_never_silent_on_empty_tool_answer(monkeypatch):
    _force_groq(monkeypatch)
    monkeypatch.setattr(brain, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(brain.llm, "complete_with_tools", lambda *a, **k: ("", []))  # tool loop gave nothing
    monkeypatch.setattr(brain.llm, "complete", lambda *a, **k: "Here's a plain answer.")
    r = brain.answer_with_tools(_avatar(), "who's in the SFF portfolio?")
    assert r["answer"] == "Here's a plain answer."  # retried plain, not silent


def test_interactive_path_no_search_for_normal_question(monkeypatch):
    _force_groq(monkeypatch)
    called = {"search": False}

    def _no(q, convo=""):
        called["search"] = True
        return "should not be used"

    monkeypatch.setattr(brain, "_web_search_answer", _no)
    monkeypatch.setattr(brain, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(brain.llm, "complete_with_tools", lambda *a, **k: ("A plain answer.", []))
    r = brain.answer_with_tools(_avatar(), "explain what a DPA is")
    assert r["answer"] == "A plain answer."
    assert called["search"] is False  # non-current question never triggers search
