"""RAG chunking and ranking tests. No network calls or API keys."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rag  # noqa: E402
from app.avatars import Avatar  # noqa: E402


def _avatar() -> Avatar:
    return Avatar(
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


def test_markdown_chunking_splits_large_sections_with_heading_context():
    body = "Manager approval is required before provisioning access. " * 80

    chunks = rag._chunk_markdown(f"# Access Provisioning\n\n{body}", "access.md")

    assert len(chunks) > 1
    assert all(c.source == "access.md" for c in chunks)
    assert all(c.section == "Access Provisioning" for c in chunks)
    assert all(c.text.startswith("Access Provisioning\n") for c in chunks)


def test_retrieve_uses_lexical_boost_for_exact_process_terms(monkeypatch):
    monkeypatch.setattr(rag, "embed", lambda *args, **kwargs: [[0.0, 0.0]])
    monkeypatch.setattr(
        rag,
        "_load",
        lambda avatar: {
            "matrix": np.array([[0.0, 0.0], [0.0, 0.0]], dtype=np.float32),
            "chunks": [
                {
                    "text": "Unrelated office lunch policy.",
                    "source": "lunch.md",
                    "section": "Food",
                },
                {
                    "text": "Manager approval is required before provisioning access.",
                    "source": "access_security_sop.md",
                    "section": "Access Provisioning",
                },
            ],
        },
    )

    out = rag.retrieve(_avatar(), "approval provisioning access", k=1)

    assert out[0].source == "access_security_sop.md"
    assert out[0].score > 0
