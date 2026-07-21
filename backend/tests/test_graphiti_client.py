"""Graphiti grounding client — the optional temporal knowledge-graph memory.

Key-free like the rest of the suite: graphiti-core is NOT installed here, so
these tests drive the module through a MOCK client (monkeypatching _get_client)
and assert the contract that matters — off by default, per-org partitioning,
best-effort ingest, and a strictly timeout-bounded recall that degrades to ""
so the live answer path never stalls.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import graphiti_client  # noqa: E402
from app.config import settings  # noqa: E402


class _FakeGraphiti:
    def __init__(self):
        self.episodes: list = []
        self.search_return: list = []
        self.search_delay: float = 0.0

    async def add_episode(self, **kwargs):
        self.episodes.append(kwargs)

    async def search(self, query, group_ids=None, num_results=None):
        if self.search_delay:
            await asyncio.sleep(self.search_delay)
        return self.search_return


class _Edge:
    def __init__(self, fact):
        self.fact = fact


@pytest.fixture
def fake(monkeypatch):
    """Enable the feature and swap the connected client for a fake."""
    monkeypatch.setattr(settings, "graphiti_enabled", True)
    monkeypatch.setattr(settings, "graphiti_uri", "neo4j://localhost:7687")
    # graphiti-core isn't installed in the key-free suite — inject a minimal
    # fake so ingest()'s `from graphiti_core.nodes import EpisodeType` resolves.
    core = types.ModuleType("graphiti_core")
    nodes = types.ModuleType("graphiti_core.nodes")

    class _EpisodeType:
        text = "text"

    nodes.EpisodeType = _EpisodeType
    monkeypatch.setitem(sys.modules, "graphiti_core", core)
    monkeypatch.setitem(sys.modules, "graphiti_core.nodes", nodes)

    client = _FakeGraphiti()
    # Simulate a client already WARMED off the hot path (init done at join) —
    # recall() uses _client directly and never inits on the live path.
    monkeypatch.setattr(graphiti_client, "_client", client)
    monkeypatch.setattr(graphiti_client, "_init_done", True)
    return client


# ───────────────────────── config gate ─────────────────────────
def test_disabled_by_default():
    assert graphiti_client.enabled() is False


def test_enabled_needs_flag_and_uri(monkeypatch):
    monkeypatch.setattr(settings, "graphiti_enabled", True)
    monkeypatch.setattr(settings, "graphiti_uri", "")
    assert graphiti_client.enabled() is False  # flag alone isn't enough
    monkeypatch.setattr(settings, "graphiti_uri", "neo4j://x")
    assert graphiti_client.enabled() is True


def test_group_id_is_per_org():
    a = graphiti_client._group_id("org_a")
    b = graphiti_client._group_id("org_b")
    assert a != b and a.startswith("org:")


# ───────────────────────── ingest ─────────────────────────
def test_ingest_stores_episode_scoped_to_org(fake):
    ok = asyncio.run(graphiti_client.ingest("org_a", "Dana owns the rollout task"))
    assert ok is True
    ep = fake.episodes[-1]
    assert ep["group_id"] == graphiti_client._group_id("org_a")
    assert "rollout" in ep["episode_body"]


def test_ingest_empty_text_is_noop(fake):
    assert asyncio.run(graphiti_client.ingest("org_a", "   ")) is False
    assert fake.episodes == []


def test_ingest_disabled_is_noop(monkeypatch):
    # Feature off → no client, no raise, just False.
    monkeypatch.setattr(settings, "graphiti_enabled", False)
    assert asyncio.run(graphiti_client.ingest("org_a", "anything")) is False


# ───────────────────────── recall ─────────────────────────
def test_recall_returns_fact_block(fake):
    fake.search_return = [_Edge("Dana owns 2 overdue tasks"),
                          _Edge("The rollout is due Friday")]
    out = asyncio.run(graphiti_client.recall("org_a", "who's overloaded?"))
    assert "Dana owns 2 overdue tasks" in out and "due Friday" in out
    assert out.count("\n") == 1  # one fact per line, two facts


def test_recall_empty_query_is_noop(fake):
    assert asyncio.run(graphiti_client.recall("org_a", "")) == ""


def test_recall_times_out_to_empty(fake, monkeypatch):
    """A slow graph must NOT delay the reply: recall returns "" past its
    budget so the caller falls back to the flat snapshot."""
    fake.search_delay = 0.5
    out = asyncio.run(
        graphiti_client.recall("org_a", "anything", timeout_s=0.05)
    )
    assert out == ""


def test_recall_swallows_errors(fake, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("graph down")

    fake.search = _boom  # type: ignore
    assert asyncio.run(graphiti_client.recall("org_a", "q")) == ""


def test_recall_disabled_is_empty(monkeypatch):
    monkeypatch.setattr(settings, "graphiti_enabled", False)
    assert asyncio.run(graphiti_client.recall("org_a", "q")) == ""


def test_recall_never_inits_on_hot_path(monkeypatch):
    """Council fix: init (connect + build_indices, unbounded) must NOT run on
    the live path. When the client isn't warm, recall returns "" and kicks a
    BACKGROUND warm-up instead of awaiting the connect."""
    monkeypatch.setattr(settings, "graphiti_enabled", True)
    monkeypatch.setattr(settings, "graphiti_uri", "neo4j://x")
    # _init_done is False (conftest reset). Stub warm() so no real task spawns
    # and assert recall neither blocks nor calls the (untimed) _get_client.
    warmed: list = []
    monkeypatch.setattr(graphiti_client, "warm", lambda: warmed.append(True))

    async def _boom_get_client():
        raise AssertionError("recall must never await init on the hot path")

    monkeypatch.setattr(graphiti_client, "_get_client", _boom_get_client)
    out = asyncio.run(graphiti_client.recall("org_a", "q"))
    assert out == "" and warmed == [True]


def test_ingest_bounds_a_hung_write(fake, monkeypatch):
    """A hung add_episode is capped by graphiti_ingest_timeout_s so the
    process-wide write lock is released, not held forever (council fix)."""
    monkeypatch.setattr(settings, "graphiti_ingest_timeout_s", 0.05)

    async def _hang(**k):
        await asyncio.sleep(0.5)

    fake.add_episode = _hang  # type: ignore
    assert asyncio.run(graphiti_client.ingest("org_a", "something")) is False


# ─────────────── Anthropic + local-stack wiring ───────────────
# These exercise the REAL graphiti-core client builders when the dependency is
# installed (it's marker-gated to Python >=3.10 in requirements); skipped
# cleanly otherwise, so the key-free suite still passes without it.
def test_embedding_dim_pinned_to_local_model():
    """graphiti-core sizes its zero-vector fallback from EMBEDDING_DIM at import;
    the module pins it to the local fastembed dimension so a text-only search
    can't mix a 1024-dim default with our 384-dim stored vectors."""
    import os
    assert os.environ.get("EMBEDDING_DIM") == str(settings.graphiti_embedding_dim)


def test_ensure_ready_reflects_client(fake):
    """ensure_ready() is the diagnostics/smoke seam: True once a client is warm."""
    assert asyncio.run(graphiti_client.ensure_ready()) is True


def test_ensure_ready_false_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "graphiti_enabled", False)
    assert asyncio.run(graphiti_client.ensure_ready()) is False


def test_construct_wires_laura_stack_no_openai(monkeypatch):
    """_construct wires Anthropic (extraction) + a LOCAL fastembed embedder + a
    LOCAL cosine reranker — no OpenAI, no new key."""
    pytest.importorskip("graphiti_core")
    from app import embeddings

    monkeypatch.setattr(settings, "anthropic_api_key", "unit-test-key-value")
    monkeypatch.setattr(settings, "graphiti_uri", "neo4j://localhost:7687")
    monkeypatch.setattr(
        embeddings, "_embed_local", lambda texts: [[0.1] * 384 for _ in texts])

    # Construct OUTSIDE a running loop so graphiti's neo4j driver doesn't
    # schedule its background index-build task (harmless, but noisy in pytest).
    client = graphiti_client._construct()
    assert type(client.llm_client).__name__ == "AnthropicClient"

    async def _exercise():
        emb, rk = client.embedder, client.cross_encoder
        assert len(await emb.create("a query")) == 384          # str
        assert len(await emb.create(["one in a list"])) == 384  # iterable → first
        batch = await emb.create_batch(["a", "b", "c"])
        assert len(batch) == 3 and all(len(v) == 384 for v in batch)
        ranked = await rk.rank("q", ["p0", "p1", "p2"])
        assert {p for p, _ in ranked} == {"p0", "p1", "p2"}  # cosine may reorder
        assert await rk.rank("q", []) == []

    asyncio.run(_exercise())
