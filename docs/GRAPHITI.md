# Graphiti — temporal knowledge-graph grounding for Petra

Optional. **Off by default**, and with it off the join/answer paths are
byte-identical to today. When on, it grounds the PM avatar (Petra) in a
*temporal* knowledge graph of the org's workspace — who owns what, due when,
who's overloaded — so she can reason about capacity instead of quoting a flat
snapshot. All the code lives in one place: [`backend/app/graphiti_client.py`](../backend/app/graphiti_client.py).

## What it does

- **Ingest (at meeting join):** the same Asana workspace snapshot Petra already
  reads is also fed into the org's graph as a temporal *episode* — off the hot
  path, best-effort. Graphiti's LLM extraction turns it into entities + dated
  relationships.
- **Recall (at answer time):** when Petra is addressed, her question hybrid-
  searches the graph and the resulting facts are folded into her grounding
  *ahead of* the flat snapshot. This is the **only** graph call on the live
  path and it is **strictly timeout-bounded** (`GRAPHITI_RECALL_TIMEOUT_S`,
  default 1.5s) — a slow or failed graph silently falls back to the snapshot,
  so the spoken reply is never delayed.
- **Multi-tenant:** every episode and search is partitioned by `group_id=org`.
  One workspace's graph is never visible to another.

## Prerequisites (what a deployment must provision)

Graphiti is a library — it needs three things you supply:

1. **A graph database.** One of:
   - **Neo4j** (default here) — e.g. a free/managed [Neo4j Aura](https://neo4j.com/cloud/aura/) instance. Set the `neo4j+s://…` URI + user + password.
   - **FalkorDB** — lighter/embeddable. Needs the driver seam (below).
   - **Zep** — the *managed* Graphiti (no DB to run yourself); the same engine hosted.
2. **`graphiti-core` installed** on the backend: `pip install graphiti-core`
   (deliberately NOT in `requirements.txt`, so the default image stays lean and
   the key-free demo is unaffected — install it on the deployment that turns
   this on).
3. **An LLM + embedder for extraction.** Graphiti's default is OpenAI, so the
   simplest path sets `OPENAI_API_KEY` in the environment. To route extraction
   through Anthropic instead (which Laura already uses), configure Graphiti's
   `llm_client`/`embedder` at the constructor — that's the documented follow-up,
   wired in the same `_get_client()` seam.

## Turn it on

Set on the backend (App Runner env), then redeploy:

```
GRAPHITI_ENABLED=true
GRAPHITI_URI=neo4j+s://<id>.databases.neo4j.io
GRAPHITI_USER=neo4j
GRAPHITI_PASSWORD=<password>
# optional tuning:
GRAPHITI_RECALL_TIMEOUT_S=1.5     # live-path budget for the graph query
GRAPHITI_RECALL_RESULTS=8         # max facts folded into a single answer
# plus the extraction LLM, e.g. OPENAI_API_KEY=...
```

Recall is gated on Petra being Asana-enabled with a connected workspace (the
join-cached `asana_live` flag), so it only activates where there's a graph to
search. If `graphiti-core` isn't installed or the DB is unreachable, the first
call logs `[graphiti] disabled — init failed (…)` and the feature stays off for
the process (sticky — a dead DB isn't retried every turn).

## ⚠ Untested live

The graphiti-core calls in `graphiti_client.py` follow its documented API but
were **unit-tested against a mock, not a live graph DB** (the dev/CI env is
key-free with no graph database). Before relying on it, smoke-test end to end
once against your real instance:

1. Install `graphiti-core`, set the env above, point at a scratch graph DB.
2. `await graphiti_client.ingest("test-org", "Dana owns the rollout, due Friday.")`
3. `await graphiti_client.recall("test-org", "what does Dana own?")` → expect a
   fact line back. Adjust the `add_episode` / `search` kwargs in
   `graphiti_client.py` if a graphiti-core version differs.

## Seams to know

- **FalkorDB / Zep driver** — swap the graph client in the single
  `_get_client()` constructor (a `graphiti_backend` setting is already carried
  for the branch).
- **Concurrency** — ingests are serialized behind `_write_lock` to sidestep
  the FalkorDB shared-driver race ([getzep/graphiti#1331](https://github.com/getzep/graphiti/issues/1331)).
- **Where facts land** — recall output is prefixed `[Knowledge graph — facts
  relevant to this question]` in Petra's brief, ahead of the Asana snapshot.

## When it's worth it

Turn this on when you want Petra to reason over *evolving, relationship-heavy*
state — capacity, task dependencies, "can Dana take this on given her load?".
For plain document Q&A it's overkill; the existing flat RAG + snapshot already
covers that.
