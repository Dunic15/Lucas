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


def test_anthropic_disables_thinking_on_completions(monkeypatch):
    """Non-streaming completions must turn extended thinking OFF so Sonnet-5's
    thinking can't eat the max_tokens budget and truncate the JSON (the
    empty-summary root cause). budget_tokens is never sent (400s on Sonnet-5)."""
    seen = {}

    class Msg:
        content = [type("T", (), {"type": "text", "text": "ok"})()]

    class Messages:
        @staticmethod
        def create(**kwargs):
            seen.update(kwargs)
            return Msg()

    class Client:
        messages = Messages()

    monkeypatch.setattr(llm, "_thinking_supported", True)
    monkeypatch.setattr(llm, "_ensure_anthropic", lambda: Client())
    assert llm._complete_anthropic("sys", "user", 4000, "claude-sonnet-5") == "ok"
    assert seen.get("thinking") == {"type": "disabled"}
    assert "budget_tokens" not in seen
    assert seen.get("max_tokens") == 4000


def test_anthropic_retries_without_thinking_when_rejected(monkeypatch):
    """A model/SDK that rejects the thinking param must not fail the completion:
    retry once without it, and stop sending it thereafter."""
    calls = []

    class Msg:
        content = [type("T", (), {"type": "text", "text": "ok"})()]

    class Messages:
        @staticmethod
        def create(**kwargs):
            calls.append(dict(kwargs))
            if "thinking" in kwargs:
                raise RuntimeError("thinking: unsupported parameter for this model")
            return Msg()

    class Client:
        messages = Messages()

    monkeypatch.setattr(llm, "_thinking_supported", True)
    monkeypatch.setattr(llm, "_ensure_anthropic", lambda: Client())
    assert llm._complete_anthropic("s", "u", 100, "legacy-model") == "ok"
    assert len(calls) == 2  # first with thinking (rejected), retry without
    assert "thinking" not in calls[1]
    assert llm._thinking_supported is False  # won't try the param again


def test_anthropic_logs_on_truncation(monkeypatch, capsys):
    """stop_reason == max_tokens means the answer was cut off; log it (the
    caller degrades to a deterministic recap) instead of silently truncating."""

    class Msg:
        stop_reason = "max_tokens"
        content = [type("T", (), {"type": "text", "text": "partial"})()]

    class Messages:
        @staticmethod
        def create(**kwargs):
            return Msg()

    class Client:
        messages = Messages()

    monkeypatch.setattr(llm, "_thinking_supported", True)
    monkeypatch.setattr(llm, "_ensure_anthropic", lambda: Client())
    assert llm._complete_anthropic("s", "u", 50, "m") == "partial"
    assert "max_tokens" in capsys.readouterr().out


def test_post_provider_split(monkeypatch):
    """BRAIN_PROVIDER_POST routes only the post-meeting path; live keeps
    BRAIN_PROVIDER. Unset -> same provider; anthropic without key -> stub."""
    from app.brain import engine as brain
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
    from app import avatars, llm
    from app.brain import engine as brain
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
    not crash; so the avatar never goes dark."""
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
    """An explicit provider= (e.g. a caller pinning Groq) must NOT be silently
    answered by the fallback model; it should raise."""
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


# ── cerebras: a first-class OpenAI-compatible provider (prod's live brain) ──


def test_compat_creds_selects_endpoint_per_provider(monkeypatch):
    """cerebras -> Cerebras base+key; groq -> Groq base+key; missing key raises."""
    monkeypatch.setattr(llm.settings, "cerebras_api_key", "csk-x")
    monkeypatch.setattr(llm.settings, "cerebras_base", "https://api.cerebras.ai/v1")
    monkeypatch.setattr(llm.settings, "groq_api_key", "gsk-y")
    assert llm._compat_creds("cerebras") == ("https://api.cerebras.ai/v1", "csk-x")
    assert llm._compat_creds("groq")[1] == "gsk-y"

    import pytest

    monkeypatch.setattr(llm.settings, "cerebras_api_key", "")
    with pytest.raises(RuntimeError, match="CEREBRAS_API_KEY"):
        llm._compat_creds("cerebras")


def test_cerebras_dispatches_through_openai_compat(monkeypatch):
    """BRAIN_PROVIDER=cerebras routes to the shared OpenAI-compatible impl with
    provider='cerebras' (not to Groq, not to Anthropic)."""
    monkeypatch.setattr(llm.settings, "brain_provider", "cerebras")
    seen = {}

    def fake_compat(system, user, max_tokens, model=None, provider="groq"):
        seen["provider"] = provider
        return "cerebras answer"

    monkeypatch.setattr(llm, "_complete_groq", fake_compat)
    assert llm.complete("s", "u") == "cerebras answer"
    assert seen["provider"] == "cerebras"


def test_cerebras_stream_falls_back_to_haiku_when_it_fails(monkeypatch):
    """A Cerebras failure trips the shared breaker and streams Haiku; parity
    with the Groq path, so the spoken avatar never goes silent."""
    monkeypatch.setattr(llm.settings, "brain_provider", "cerebras")
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "k")

    def boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "_stream_groq", boom)
    monkeypatch.setattr(llm, "_stream_anthropic", lambda s, u, mt, m: iter(["hi from haiku"]))

    assert list(llm.stream_complete("s", "u")) == ["hi from haiku"]
