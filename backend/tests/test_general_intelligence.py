"""Live path: general-assistant behavior; search routing + conditional grounding."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain import engine as brain  # noqa: E402
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
    monkeypatch.setattr(settings, "anthropic_api_key", "k")  # web search runs on Claude now
    monkeypatch.setattr(settings, "live_search_enabled", True)


def test_search_questions_route_to_search_model(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", True)
    assert brain._live_model("search the internet for the latest AI news") == settings.live_search_model
    assert brain._live_model("what's the weather today in Milan?") == settings.live_search_model
    assert brain._live_model("who won the match yesterday?") == settings.live_search_model


def test_normal_questions_keep_fast_model(monkeypatch):
    monkeypatch.setattr(settings, "brain_provider", "groq")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", True)
    assert brain._live_model("what are we missing before go-live?") == settings.brain_model_fast
    assert brain._live_model("explain what a DPA is") == settings.brain_model_fast


def test_search_routes_regardless_of_live_provider(monkeypatch):
    """Web search runs on Claude regardless of the fast-path BRAIN_PROVIDER."""
    monkeypatch.setattr(settings, "brain_provider", "groq")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", True)
    assert brain._live_model("latest news please") == settings.live_search_model


def test_search_routing_respects_flag_and_key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")  # no Claude -> no search
    assert brain._live_model("latest news please") == settings.brain_model_fast
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "live_search_enabled", False)
    assert brain._live_model("latest news please") == settings.brain_model_fast


def test_live_route_tiers_by_task(monkeypatch):
    _force_groq(monkeypatch)
    monkeypatch.setattr(settings, "brain_model_complex", "claude-haiku-4-5")
    # simple/chat -> fast Groq
    assert brain._live_route("how are you?") == ("groq", settings.brain_model_fast)
    # web search -> Claude native web_search (the "search" pseudo-provider)
    assert brain._live_route("what is the latest news today?") == ("search", settings.live_search_model)
    # complex reasoning -> Claude
    assert brain._live_route("compare bootstrapping vs raising a seed round") == ("anthropic", "claude-haiku-4-5")
    assert brain._live_route("analyze the tradeoffs here") == ("anthropic", "claude-haiku-4-5")


def test_live_route_no_anthropic_key_stays_on_fast(monkeypatch):
    _force_groq(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "")  # no Claude available
    assert brain._live_route("compare these two options in detail") == ("groq", settings.brain_model_fast)


def test_prompt_is_assistant_first():
    p = brain.ANSWER_STREAM_SYSTEM
    assert "capable general assistant FIRST" in p
    assert "Never refuse just because it isn't in the documents" in p
    assert "SKIP" in p  # silence gate preserved


# ── web search on the interactive /live/act path (phase 2) ──
def test_web_search_answer_empty_on_refusal_or_blank(monkeypatch):
    monkeypatch.setattr(brain.llm, "web_search", lambda *a, **k: "I'm not able to browse the web right now.")
    assert brain._web_search_answer("latest news") == ""
    monkeypatch.setattr(brain.llm, "web_search", lambda *a, **k: "")
    assert brain._web_search_answer("latest news") == ""
    monkeypatch.setattr(brain.llm, "web_search", lambda *a, **k: "Milan is sunny, 24°C; from a quick search.")
    assert "quick search" in brain._web_search_answer("weather Milan today")


def test_interactive_path_uses_web_search_for_current_info(monkeypatch):
    _force_groq(monkeypatch)
    monkeypatch.setattr(
        brain, "_web_search_answer", lambda q, convo="": "Milan is sunny; from a quick search."
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
    # "who's in the SFF portfolio?" matches the web-search intent (sff|portfolio),
    # so with keys "present" answer_with_tools tries _web_search_answer FIRST -
    # unstubbed, that's a live Anthropic call that only "passed" because the fake
    # key errors out. Stub it to "" (search flaked) so the test exercises exactly
    # its subject: the empty-tool-answer -> plain-retry path, with zero network.
    monkeypatch.setattr(brain, "_web_search_answer", lambda q, convo="": "")
    monkeypatch.setattr(brain, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(brain.llm, "complete_with_tools", lambda *a, **k: ("", []))  # tool loop gave nothing
    monkeypatch.setattr(brain.llm, "complete", lambda *a, **k: "Here's a plain answer.")
    r = brain.answer_with_tools(_avatar(), "who's in the SFF portfolio?")
    assert r["answer"] == "Here's a plain answer."  # retried plain, not silent


def test_interactive_path_survives_tool_endpoint_error(monkeypatch):
    """Groq's tool endpoint 429s with no fallback; answer_with_tools must catch it
    and retry a plain answer (Haiku-capable), not raise and go silent."""
    _force_groq(monkeypatch)
    monkeypatch.setattr(brain, "retrieve", lambda *a, **k: [])

    def boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(brain.llm, "complete_with_tools", boom)
    monkeypatch.setattr(brain.llm, "complete", lambda *a, **k: "Plain fallback answer.")
    r = brain.answer_with_tools(_avatar(), "tell me a joke")
    assert r["answer"] == "Plain fallback answer."


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
