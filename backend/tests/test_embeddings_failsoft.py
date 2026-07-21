"""Boot must never depend on HuggingFace being up.

2026-07-16: a deploy rolled back because startup hung downloading the
fastembed model (HF returning 504s) until App Runner's health check killed
it. The local provider now fails SOFT: hash vectors for this process, and the
index signature stamps the EFFECTIVE provider so a hash-built index rebuilds
with real vectors on the next healthy boot; query and index vectors can
never silently disagree across restarts.
"""
from __future__ import annotations

import json

import pytest

from app import embeddings, rag
from app.config import settings


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(embeddings, "_local_failed", False)
    monkeypatch.setattr(embeddings, "_fastembed_model", None)


def _boom_model(monkeypatch):
    """Simulate the HF outage: model construction raises."""
    import fastembed

    class _Down:
        def __init__(self, *a, **k):
            raise TimeoutError("HuggingFace 504")

    monkeypatch.setattr(fastembed, "TextEmbedding", _Down)


def test_local_falls_back_to_hash_instead_of_raising(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "local")
    _boom_model(monkeypatch)
    vectors = embeddings.embed(["boot must survive this"])
    assert vectors and len(vectors[0]) > 0  # hash vectors, no exception
    assert embeddings.provider_signature() == "hash"


def test_failure_cached_for_process(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "local")
    _boom_model(monkeypatch)
    embeddings.embed(["first attempt trips the fallback"])
    calls = []

    import fastembed

    class _Spy:
        def __init__(self, *a, **k):
            calls.append(1)

    monkeypatch.setattr(fastembed, "TextEmbedding", _Spy)
    embeddings.embed(["second call must not retry the download"])
    assert calls == []  # no second construction attempt this process


def test_signature_healthy_is_configured_provider(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "local")
    assert embeddings.provider_signature() == "local"
    monkeypatch.setattr(settings, "embedding_provider", "hash")
    assert embeddings.provider_signature() == "hash"


def test_warmup_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "local")
    _boom_model(monkeypatch)
    embeddings.warmup()  # must not raise
    assert embeddings.provider_signature() == "hash"


def test_index_written_during_outage_rebuilds_after_recovery(
    monkeypatch, tmp_path
):
    """The self-heal property: an index stamped 'hash' (outage boot) is stale
    for a healthy 'local' process; signature mismatch forces the rebuild."""
    monkeypatch.setattr(settings, "embedding_provider", "local")
    monkeypatch.setattr(embeddings, "_local_failed", True)  # outage boot

    doc = tmp_path / "d.md"
    doc.write_text("# T\n\nsome content")
    idx = tmp_path / "i.json"
    chunks = rag._collect_chunks([doc])
    rag._write_index(idx, chunks, [doc])
    assert json.loads(idx.read_text())["provider"] == "hash"
    assert rag._index_is_current(idx, [doc]) is True  # consistent while degraded

    monkeypatch.setattr(embeddings, "_local_failed", False)  # model recovered
    assert rag._index_is_current(idx, [doc]) is False  # forces real rebuild
