"""LLM plumbing: response parsing must survive adaptive-thinking models."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import llm  # noqa: E402


def test_anthropic_extraction_skips_thinking_blocks(monkeypatch):
    """Sonnet 5+ runs adaptive thinking by default: content[0] is a thinking
    block with NO .text attribute (pydantic raises AttributeError). The text
    must come from the first text block, wherever it sits."""

    class ThinkingBlock:  # deliberately has no .text
        type = "thinking"

    class TextBlock:
        type = "text"
        text = "the actual answer"

    class Msg:
        content = [ThinkingBlock(), TextBlock()]

    class Messages:
        @staticmethod
        def create(**kwargs):
            return Msg()

    class Client:
        messages = Messages()

    monkeypatch.setattr(llm, "_ensure_anthropic", lambda: Client())
    assert llm._complete_anthropic("sys", "user", 100, None) == "the actual answer"


def test_anthropic_extraction_empty_when_no_text_block(monkeypatch):
    class ThinkingBlock:
        type = "thinking"

    class Msg:
        content = [ThinkingBlock()]

    class Messages:
        @staticmethod
        def create(**kwargs):
            return Msg()

    class Client:
        messages = Messages()

    monkeypatch.setattr(llm, "_ensure_anthropic", lambda: Client())
    assert llm._complete_anthropic("sys", "user", 100, None) == ""


def test_post_provider_split(monkeypatch):
    """BRAIN_PROVIDER_POST routes only the post-meeting path; live keeps
    BRAIN_PROVIDER. Unset -> same provider; anthropic without key -> stub."""
    from app import brain
    from app.config import settings

    monkeypatch.setattr(settings, "brain_provider", "groq")
    monkeypatch.setattr(settings, "brain_provider_post", "")
    assert brain.post_provider() == "groq"

    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    assert brain.post_provider() == "anthropic"
    assert brain.effective_provider() == "groq"  # live path untouched

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert brain.post_provider() == "stub"  # never crash keyless


def test_post_meeting_uses_post_provider(monkeypatch):
    """post_meeting sends its completion through the post provider."""
    from app import avatars, brain, llm
    from app.config import settings

    monkeypatch.setattr(settings, "brain_provider", "groq")
    monkeypatch.setattr(settings, "brain_provider_post", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "k")

    seen = {}

    def fake_complete(system, user, *, max_tokens=800, model=None, provider=None):
        seen["provider"] = provider
        return '{"summary": "s", "decisions": [], "actions": [], "risks": [], "follow_up_email": {}}'

    monkeypatch.setattr(brain.llm, "complete", fake_complete)
    monkeypatch.setattr(brain, "retrieve", lambda avatar, q, k=6: [])
    avatar = avatars.load("laura")
    artifact = brain.post_meeting(avatar, "Ana: kickoff for the Acme onboarding.")
    assert seen["provider"] == "anthropic"
    assert artifact["summary"] == "s"


def test_complete_falls_back_to_haiku_when_groq_fails(monkeypatch):
    """Groq 429 (rate limit) on the default path must fall back to Claude Haiku,
    not crash — so the avatar never goes dark."""
    monkeypatch.setattr(llm.settings, "brain_provider", "groq")
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "k")

    def boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")

    seen = {}

    def fake_anthropic(system, user, max_tokens, model=None):
        seen["model"] = model
        return "haiku answer"

    monkeypatch.setattr(llm, "_complete_groq", boom)
    monkeypatch.setattr(llm, "_complete_anthropic", fake_anthropic)

    assert llm.complete("s", "u") == "haiku answer"
    assert seen["model"] == llm._FALLBACK_MODEL


def test_explicit_provider_does_not_fall_back(monkeypatch):
    """An explicit provider= (e.g. the web-search compound call) must NOT be
    silently answered by the non-searching fallback — it should raise."""
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "k")
    monkeypatch.setattr(llm, "_complete_groq", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429")))
    import pytest
    with pytest.raises(RuntimeError):
        llm.complete("s", "u", provider="groq")


def test_stream_falls_back_to_haiku_when_groq_fails(monkeypatch):
    monkeypatch.setattr(llm.settings, "brain_provider", "groq")
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "k")

    def boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "_stream_groq", boom)
    monkeypatch.setattr(llm, "_stream_anthropic", lambda s, u, mt, m: iter(["hi from haiku"]))

    assert list(llm.stream_complete("s", "u")) == ["hi from haiku"]
