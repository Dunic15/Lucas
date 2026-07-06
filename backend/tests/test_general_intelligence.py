"""Live path: general-assistant behavior — search routing + conditional grounding."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import brain  # noqa: E402
from app.config import settings  # noqa: E402


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
