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

from .config import settings

# One Graphiti instance per process, connected + indexed once. Import + connect
# are lazy (only when enabled), so a deployment WITHOUT graphiti-core installed
# or a graph DB configured pays nothing and behaves exactly as before.
_client = None
_init_lock = asyncio.Lock()
_init_done = False
_unavailable = False  # sticky: a failed import/connect disables the feature
# add_episode reassigns the shared FalkorDB driver per group_id (getzep/graphiti
# issue #1331) — serialize writes so concurrent orgs can't cross-contaminate.
_write_lock = asyncio.Lock()


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
            # Lazy import: graphiti-core is an OPTIONAL dependency. Absent ⇒
            # the feature disables cleanly instead of failing the app import.
            from graphiti_core import Graphiti  # type: ignore

            # Neo4j default constructor. FalkorDB/Zep swap the driver HERE (a
            # single, documented seam) — see docs/GRAPHITI.md.
            client = Graphiti(
                settings.graphiti_uri.strip(),
                settings.graphiti_user.strip() or "neo4j",
                settings.graphiti_password,
            )
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

        async with _write_lock:
            await client.add_episode(
                name=name,
                episode_body=text[:20000],
                source=EpisodeType.text,
                source_description=source_description,
                reference_time=datetime.now(timezone.utc),
                group_id=_group_id(org_id),
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
    (one fact per line) or "" on empty/timeout/failure. Strictly
    timeout-bounded — the live answer path falls back to the flat snapshot
    rather than wait on the graph."""
    query = (query or "").strip()
    if not query:
        return ""
    client = await _get_client()
    if client is None:
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
    global _client, _init_done, _unavailable
    _client = None
    _init_done = False
    _unavailable = False
