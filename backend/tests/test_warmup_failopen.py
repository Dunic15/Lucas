"""Boot warm-up fail-open (live incident 2026-07-24).

The post-deploy index warm-up took 1480s (HF model download + full re-embed
after a docs change) and a meeting started inside that window: every live
retrieve queued behind the rebuild for 106-121s, every answer was cancelled,
and the avatar sat mute. While rag.is_warming(), the live retrieval path
returns [] immediately — an answer grounded in the briefs beats two minutes
of silence. Key-free.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.avatar.avatars import load  # noqa: E402
from app.brain import engine, rag  # noqa: E402


def test_retrieve_for_skips_rag_while_warming():
    avatar = load("petra")
    rag.set_warming(True)
    try:
        hits = engine._retrieve_for(avatar, "what are we missing before go-live?",
                                    "", 4, org_id="org-x")
        assert hits == []  # instant, ungated answer — never a 2-minute queue
        # self-questions skip too (retrieve_about would also hit the index)
        assert engine._retrieve_for(avatar, "which tools can you use?",
                                    "", 4) == []
    finally:
        rag.set_warming(False)


def test_retrieval_returns_after_warmup():
    avatar = load("petra")
    rag.set_warming(False)
    hits = engine._retrieve_for(avatar, "what are we missing before go-live?",
                                "", 2)
    assert hits  # normal grounding is back the moment the warm-up ends


def test_warming_flag_roundtrip():
    assert rag.is_warming() is False
    rag.set_warming(True)
    assert rag.is_warming() is True
    rag.set_warming(False)
    assert rag.is_warming() is False
