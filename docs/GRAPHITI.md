# Graphiti: temporal knowledge-graph grounding for Petra

Optional. **Off by default**, and with it off the join/answer paths are
byte-identical to today. When on, it grounds the PM avatar (Petra) in a
*temporal* knowledge graph of the org's workspace, who owns what, due when,
who's overloaded, so she can reason about capacity instead of quoting a flat
snapshot. All the code lives in one place: [`backend/app/graphiti_client.py`](../backend/app/graphiti_client.py).

## What it does

- **Ingest (at meeting join):** the same Asana workspace snapshot Petra already
  reads is also fed into the org's graph as a temporal *episode*: off the hot
  path, best-effort. Graphiti's LLM extraction turns it into entities + dated
  relationships.
- **Recall (at answer time):** when Petra is addressed, her question hybrid-
  searches the graph and the resulting facts are folded into her grounding
  *ahead of* the flat snapshot. This is the **only** graph call on the live
  path and it is **strictly timeout-bounded** (`GRAPHITI_RECALL_TIMEOUT_S`,
  default 1.5s); a slow or failed graph silently falls back to the snapshot,
  so the spoken reply is never delayed.
- **Multi-tenant:** every episode and search is partitioned by `group_id=org`.
  One workspace's graph is never visible to another.

## Prerequisites (what a deployment must provision)

Graphiti is a library; it needs three things you supply:

1. **A graph database.** One of:
   - **Neo4j** (default here): e.g. a free/managed [Neo4j Aura](https://neo4j.com/cloud/aura/) instance. Set the `neo4j+s://…` URI + user + password.
   - **FalkorDB**: lighter/embeddable. Needs the driver seam (below).
   - **Zep**: the *managed* Graphiti (no DB to run yourself); the same engine hosted.
2. **`graphiti-core` installed**: it's in `requirements.txt` as
   `graphiti-core>=0.29,<0.30; python_version >= "3.10"`. The environment
   marker is load-bearing: **no graphiti-core release supports Python <3.10**,
   and on 2026-07-20 the unmarked line made `pip install -r` fail on the App
   Runner build runtime → every deploy rolled back (#319). With the marker an
   old runtime skips the dep (feature off, deploy safe); a ≥3.10 runtime
   installs it. Lazy-imported, so it's inert when the feature is off.
3. **No extra LLM/embedder key.** Extraction runs on **Laura's own stack**: no
   OpenAI, no new vendor key (`graphiti_client._construct`):
   - **LLM** = Anthropic, reusing the existing `ANTHROPIC_API_KEY`
     (`GRAPHITI_LLM_MODEL` = Sonnet for extraction, `GRAPHITI_LLM_SMALL_MODEL` =
     Haiku for cheap dedup/summarize).
   - **Embeddings** = Laura's local `fastembed` model (same one the RAG uses,
     384-dim; pinned into graphiti-core as `EMBEDDING_DIM`).
   - **Reranker** = a local cosine reranker over those same embeddings.

   So the ONLY things a deployment provisions beyond what Laura already has are
   the **graph DB** (Neo4j creds) and a **Python ≥3.10 build runtime**.

## Turn it on

Set on the backend (App Runner env), then redeploy:

```
GRAPHITI_ENABLED=true
GRAPHITI_URI=neo4j+s://<id>.databases.neo4j.io
GRAPHITI_USER=neo4j
GRAPHITI_PASSWORD=<password>
# ANTHROPIC_API_KEY is already set for the brain: extraction reuses it.
# optional tuning:
GRAPHITI_RECALL_TIMEOUT_S=1.5     # live-path budget for the graph query
GRAPHITI_RECALL_RESULTS=8         # max facts folded into a single answer
GRAPHITI_LLM_MODEL=claude-sonnet-5        # extraction quality
GRAPHITI_LLM_SMALL_MODEL=claude-haiku-4-5 # cheap dedup/summarize
GRAPHITI_EMBEDDING_DIM=384                 # match your fastembed model
```

Recall is gated on Petra being Asana-enabled with a connected workspace (the
join-cached `asana_live` flag), so it only activates where there's a graph to
search. If `graphiti-core` isn't installed or the DB is unreachable, the first
call logs `[graphiti] disabled, init failed (…)` and the feature stays off for
the process (sticky, a dead DB isn't retried every turn; a restart/redeploy
retries after you fix the cause).

## Verify a deploy (smoke test)

The wiring is validated against a real Neo4j Aura, but **verify YOUR deploy**
once after setting the env above; no live meeting needed:

```
curl -sS -H "Authorization: Bearer $LAURA_API_TOKEN" \
  "$PUBLIC_BASE_URL/health/graphiti?run=1" | jq
```

- Without `?run=1` it's a cheap status check: `enabled`, `configured`,
  `graphiti_core` (version or `MISSING`), `anthropic_key_set`, `embedding_dim`,
  and **`python`**: the build runtime version. If `python` reads `3.9.x` or
  older, that is why `graphiti_core` is MISSING (the marker skipped it): bump
  the App Runner runtime to ≥3.10 or switch the service to the repo's
  `Dockerfile` (python:3.12-slim).
- With `?run=1` it runs a **real ingest→recall** against a dedicated `__smoke__`
  group (never touches a real org's graph) and returns `live.ok:true` with the
  recalled fact lines when the round-trip works; `live.ok:false` carries a
  `stage`/`hint` and a 503.

## Seams to know

- **FalkorDB / Zep driver**: swap the graph client in the single
  `_get_client()` constructor (a `graphiti_backend` setting is already carried
  for the branch).
- **Concurrency**: ingests are serialized behind `_write_lock` to sidestep
  the FalkorDB shared-driver race ([getzep/graphiti#1331](https://github.com/getzep/graphiti/issues/1331)).
- **Where facts land**, recall output is prefixed `[Knowledge graph, facts
  relevant to this question]` in Petra's brief, ahead of the Asana snapshot.

## When it's worth it

Turn this on when you want Petra to reason over *evolving, relationship-heavy*
state; capacity, task dependencies, "can Dana take this on given her load?".
For plain document Q&A it's overkill; the existing flat RAG + snapshot already
covers that.
