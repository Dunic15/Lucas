"""Graphiti grounding — an optional temporal knowledge-graph memory for the
PM avatar (Petra).

OFF by default and byte-identical to today when unconfigured. This is a thin,
best-effort wrapper around graphiti-core (an EXTERNAL dependency plus a graph
database — Neo4j or FalkorDB — that a deployment must provision; see
docs/GRAPHITI.md). When GRAPHITI_ENABLED is set, the graph-DB creds are
present, AND graphiti-core is installed, it does two things:

  * ingest()  — feeds the org's Asana snapshot (or any text) into the graph as
                a temporal "episode" at meeting join, off the hot path.
  * recall()  — at answer time, hybrid-searches the graph for the question and
                returns a compact fact block to fold into the avatar's brief,
                bounded by a STRICT timeout so the live path never stalls.

Multi-tenant by org: every episode + search is scoped to group_id=org, so one
workspace's graph is never visible to another.

Every path is best-effort and NEVER raises into the caller: a missing
dependency, an unprovisioned/slow graph DB, or a failed search degrades to
today's flat-snapshot behavior. Latency is the product — recall() is the only
call on the live path and it is timeout-bounded; a miss falls back silently.

NOTE (untested-live): the graphiti-core calls here follow its documented API
but were unit-tested against a mock, not a live graph DB. First activation
should smoke-test ingest()+recall() end to end (docs/GRAPHITI.md).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ..config import settings

# One Graphiti instance per process, connected + indexed once. Import + connect
# are lazy (only when enabled), so a deployment WITHOUT graphiti-core installed
# or a graph DB configured pays nothing and behaves exactly as before.
_client = None
_init_lock = asyncio.Lock()
_init_done = False
_unavailable = False  # sticky: a failed import/connect disables the feature
_warm_task = None     # strong ref to the in-flight background warm-up
# add_episode reassigns the shared FalkorDB driver per group_id (getzep/graphiti
# issue #1331) — serialize writes so concurrent orgs can't cross-contaminate.
_write_lock = asyncio.Lock()


def warm() -> None:
    """Kick a BACKGROUND graph-DB init if the client isn't ready and none is in
    flight. This is how the live path avoids ever awaiting init (connect +
    build_indices — an unbounded network round-trip): recall() calls warm() and
    skips the graph for the current turn, so a slow/hung DB can never stall the
    spoken reply. Idempotent; no-op once warm or if the loop isn't running."""
    global _warm_task
    if not enabled() or _init_done:
        return
    if _warm_task is not None and not _warm_task.done():
        return
    try:
        _warm_task = asyncio.create_task(_get_client())
    except RuntimeError:  # no running loop — nothing to warm on
        pass


def enabled() -> bool:
    """The feature is on only when configured AND not stickily disabled by a
    prior failed init (missing dependency / unreachable graph DB)."""
    return bool(
        settings.graphiti_enabled
        and settings.graphiti_uri.strip()
        and not _unavailable
    )


def _group_id(org_id: str) -> str:
    """Per-tenant graph partition. One org's episodes/search never touch
    another's."""
    return f"org:{(org_id or settings.demo_org_id).strip()}"


def _construct():
    """Build the Graphiti client wired to LAURA'S OWN stack — no OpenAI, no new
    vendor key: Anthropic for LLM extraction (the existing anthropic_api_key),
    Laura's local fastembed for embeddings, and a local cosine reranker. All
    graphiti-core imports are lazy so a deployment without the optional
    dependency (or with the feature off) pays nothing. Validated end-to-end
    against a real Neo4j Aura instance before shipping (see docs/GRAPHITI.md)."""
    import os
    import sys

    from ..config import REPO_ROOT

    # graphiti-core is installed into a SIDECAR dir by the App Runner build,
    # deliberately NOT via requirements.txt: adding it to the flat
    # `pip install --target .` main install broke the App Runner boot (health
    # check failed → rollback; incident 2026-07-20). Its deps are pure-Python
    # but their presence in the boot-critical install crashed startup. Putting
    # them in their own dir and adding it to sys.path ONLY here (lazy, feature-
    # on) keeps the boot install byte-identical to the known-good one.
    _libs = str(REPO_ROOT / "_graphiti_libs")
    if os.path.isdir(_libs) and _libs not in sys.path:
        sys.path.append(_libs)

    from graphiti_core import Graphiti  # type: ignore
    from graphiti_core.cross_encoder.client import CrossEncoderClient  # type: ignore
    from graphiti_core.embedder.client import EmbedderClient  # type: ignore
    from graphiti_core.llm_client.anthropic_client import AnthropicClient  # type: ignore
    from graphiti_core.llm_client.config import LLMConfig  # type: ignore

    from .. import embeddings

    class _LocalEmbedder(EmbedderClient):
        """graphiti-core embedder backed by Laura's local fastembed model, so
        the graph shares the RAG embedding space and needs no embeddings API."""
        async def create(self, input_data):
            text = input_data if isinstance(input_data, str) else (
                next(iter(input_data), "") if input_data else "")
            return embeddings._embed_local([str(text)])[0]

        async def create_batch(self, input_data_list):
            return embeddings._embed_local([str(t) for t in input_data_list])

    class _LocalReranker(CrossEncoderClient):
        """Key-free reranker: cosine similarity over the same local embeddings
        (avoids graphiti-core's default OpenAI reranker / a heavy BGE model)."""
        async def rank(self, query, passages):
            passages = list(passages or [])
            if not passages:
                return []
            import numpy as np

            vecs = embeddings._embed_local([query] + passages)
            q = np.asarray(vecs[0], dtype=float)
            qn = float(np.linalg.norm(q)) + 1e-9
            scored = []
            for passage, v in zip(passages, vecs[1:]):
                vv = np.asarray(v, dtype=float)
                scored.append(
                    (passage, float(q.dot(vv) / (qn * (float(np.linalg.norm(vv)) + 1e-9))))
                )
            return sorted(scored, key=lambda x: x[1], reverse=True)

    llm = AnthropicClient(LLMConfig(
        api_key=settings.anthropic_api_key,
        model=settings.graphiti_llm_model,
        small_model=settings.graphiti_llm_small_model,
    ))
    # Neo4j default driver; FalkorDB/Zep swap it HERE (documented seam).
    return Graphiti(
        settings.graphiti_uri.strip(),
        settings.graphiti_user.strip() or "neo4j",
        settings.graphiti_password,
        llm_client=llm,
        embedder=_LocalEmbedder(),
        cross_encoder=_LocalReranker(),
    )


async def _get_client():
    """The process Graphiti singleton, connected + indexed once. Returns None
    when the feature is off or the dependency/DB is unavailable (sticky, so a
    dead graph DB is not retried on every turn)."""
    global _client, _init_done, _unavailable
    if not enabled():
        return None
    if _init_done:
        return _client
    async with _init_lock:
        if _init_done:
            return _client
        try:
            client = _construct()
            await client.build_indices_and_constraints()
            _client = client
        except Exception as e:  # noqa: BLE001 — must never break the app
            print(f"[graphiti] disabled — init failed ({type(e).__name__})", flush=True)
            _unavailable = True
            _client = None
        _init_done = True
    return _client


async def ingest(
    org_id: str, text: str, *, name: str = "workspace snapshot",
    source_description: str = "asana",
) -> bool:
    """Feed one episode into the org's graph. Best-effort and off the hot path;
    serialized to sidestep the FalkorDB shared-driver race. Returns True when an
    episode was stored."""
    text = (text or "").strip()
    if not text:
        return False
    client = await _get_client()
    if client is None:
        return False
    try:
        from graphiti_core.nodes import EpisodeType  # type: ignore

        # Bound the write: _write_lock is process-wide (FalkorDB race), so a
        # hung add_episode against a dead DB must NOT freeze every org's
        # ingestion — cap it and release the lock (council 2026-07-20).
        async with _write_lock:
            await asyncio.wait_for(
                client.add_episode(
                    name=name,
                    episode_body=text[:20000],
                    source=EpisodeType.text,
                    source_description=source_description,
                    reference_time=datetime.now(timezone.utc),
                    group_id=_group_id(org_id),
                ),
                timeout=settings.graphiti_ingest_timeout_s,
            )
        return True
    except Exception as e:  # noqa: BLE001 — ingest never breaks the join
        print(f"[graphiti] ingest skipped ({type(e).__name__})", flush=True)
        return False


def _fact_of(result) -> str:
    """The human-readable fact from a Graphiti search hit (EntityEdge.fact),
    tolerating a dict shape too."""
    fact = getattr(result, "fact", None)
    if fact is None and isinstance(result, dict):
        fact = result.get("fact")
    return str(fact).strip() if fact else ""


async def recall(
    org_id: str, query: str, *, timeout_s: float | None = None,
    num_results: int | None = None,
) -> str:
    """Hybrid-search the org's graph for `query`; return a compact fact block
    (one fact per line) or "" on empty/not-ready/timeout/failure.

    Live-path safe (council 2026-07-20): init (connect + build_indices — an
    unbounded network round-trip) NEVER runs here. If the client isn't warm
    yet, kick a background warm-up and return "" for this turn (the caller
    falls back to the flat snapshot); the next question uses the warmed client.
    Only the already-connected search() is awaited, and it is strictly
    timeout-bounded — so a slow/hung graph DB can never stall the spoken reply.
    """
    query = (query or "").strip()
    if not query or not enabled():
        return ""
    if not _init_done:
        # Not connected yet — warm in the background, skip the graph this turn.
        warm()
        return ""
    client = _client
    if client is None:  # init completed but failed (sticky _unavailable)
        return ""
    budget = timeout_s if timeout_s is not None else settings.graphiti_recall_timeout_s
    limit = num_results if num_results is not None else settings.graphiti_recall_results
    try:
        results = await asyncio.wait_for(
            client.search(query, group_ids=[_group_id(org_id)], num_results=limit),
            timeout=budget,
        )
    except asyncio.TimeoutError:
        print("[graphiti] recall timed out — using snapshot", flush=True)
        return ""
    except Exception as e:  # noqa: BLE001 — a failed graph never blocks a reply
        print(f"[graphiti] recall skipped ({type(e).__name__})", flush=True)
        return ""
    facts = [f"- {f}" for f in (_fact_of(r) for r in (results or [])) if f]
    return "\n".join(facts[:limit])


def reset_for_tests() -> None:
    """Clear the process singleton + sticky flags — mirrors the other clients'
    conftest resets so state can't leak between tests."""
    global _client, _init_done, _unavailable, _warm_task
    _client = None
    _init_done = False
    _unavailable = False
    _warm_task = None
