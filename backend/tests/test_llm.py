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
