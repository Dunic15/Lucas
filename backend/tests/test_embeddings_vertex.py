"""Vertex (Gemini) embedding provider — offline unit tests.

Verifies the provider builds the right regional Vertex predict URL, sends the
correct asymmetric task_type, preserves order, batches, and reuses the existing
service-account auth (no new key). Never hits the network.
"""
from unittest.mock import patch

import pytest

from app.brain import embeddings as E
from app.config import settings


class _Resp:
    def __init__(self, n):
        self._n = n

    def raise_for_status(self):
        pass

    def json(self):
        return {"predictions": [{"embeddings": {"values": [0.1, 0.2, 0.3]}}
                                for _ in range(self._n)]}


@pytest.fixture
def vertex_cfg(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "vertex")
    monkeypatch.setattr(settings, "vertex_project", "proj123")
    monkeypatch.setattr(settings, "vertex_location", "global")
    monkeypatch.setattr(settings, "embedding_model", "voyage-3")  # wrong-family default
    monkeypatch.setattr("app.brain.llm._vertex_token", lambda: "tok-abc")


def test_vertex_embed_builds_regional_url_and_task_type(vertex_cfg):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        seen["task"] = json["instances"][0]["task_type"]
        seen["n"] = len(json["instances"])
        return _Resp(seen["n"])

    with patch("httpx.post", fake_post):
        docs = E.embed(["hello world", "second doc"], input_type="document")
        E.embed(["a query"], input_type="query")

    assert len(docs) == 2 and docs[0] == [0.1, 0.2, 0.3]
    # global → regional us-central1 (embedding models aren't on the global host)
    assert "us-central1-aiplatform.googleapis.com" in seen["url"]
    # voyage-name default replaced by a real Vertex model
    assert "text-embedding-004:predict" in seen["url"]
    assert seen["auth"] == "Bearer tok-abc"
    assert seen["task"] == "RETRIEVAL_QUERY"  # the last call was a query


def test_vertex_embed_batches_over_100(vertex_cfg):
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _Resp(len(json["instances"]))

    with patch("httpx.post", fake_post):
        out = E.embed([f"doc {i}" for i in range(250)])
    assert len(out) == 250
    assert calls["n"] == 3  # 100 + 100 + 50


def test_vertex_builder_requires_project_and_token(monkeypatch):
    # The low-level builder still raises on misconfig (surfaces config errors).
    monkeypatch.setattr(settings, "vertex_project", "")
    monkeypatch.setattr("app.brain.llm._vertex_token", lambda: "")
    with pytest.raises(RuntimeError):
        E._embed_vertex(["x"], "document")


def test_embed_failsoft_on_hosted_provider_error(monkeypatch):
    """A hosted provider failure (429/outage/misconfig) must NOT raise from
    embed() — it falls back to local so the live path never 500s (the mute-
    Petra incident 2026-07-22)."""
    monkeypatch.setattr(settings, "embedding_provider", "vertex")

    def boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(E, "_embed_vertex", boom)
    monkeypatch.setattr(E, "_embed_local", lambda texts: [[0.5] * 8 for _ in texts])
    out = E.embed(["hello"])  # must not raise
    assert out == [[0.5] * 8]
