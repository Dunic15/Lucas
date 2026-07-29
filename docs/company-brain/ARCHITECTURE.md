# Company Brain data plane — architecture decision document

Status: accepted (v0 vertical slice built in this repo, branch
`restructure/repo-structure`). Companions: `THREAT-MODEL.md`, `CONTRACTS.md`,
`MSGRAPH-PERMISSIONS.md`, `AWS-DEPLOYMENT.md`, `CONNECTOR-ROADMAP.md`,
`RISKS-AND-MILESTONES.md`.

## Context

Laura needs a **data plane** that answers "what does the company know?" with
grounded, cited, permission-correct answers — kept strictly separate from the
**action plane** (OpenClaw + Pipedream Connect), which decides and executes
after approval. Microsoft Graph is the first production connector; the
contracts must be source-agnostic.

A Company Brain M1 already exists in `backend/app/knowledge/` (migration
`0010_company_brain`): six FORCE-RLS Postgres tables, a claim/lease job
worker, S3-or-disk raw storage, Postgres FTS + an in-memory per-(org,avatar)
semantic index, upload + shallow Google Drive ingestion. What M1 lacks
(verified by repo recon): any principal/group ACL model, per-user retrieval
filtering, durable sync checkpoints/delta tokens, external-ID document
identity, remote-deletion propagation, permission-change tracking, a
connector contract with an injectable transport, fused hybrid retrieval, and
audit events.

## Decisions

### D1 — Extend `backend/app/knowledge/`, don't build a parallel plane
M2 lives in the same package: it inherits the flag gate
(`knowledge.enabled()` = `COMPANY_BRAIN_ENABLED` **and** control plane
configured — the key-free demo never sees any of it), the FORCE-RLS tenancy
discipline, the `knowledge_sync_jobs` claim/lease worker already dispatched
from `main.py`'s lifespan, and the `/org/knowledge/*` + `/dashboard/knowledge/*`
router (zero `main.py` edits). The action plane (`backend/app/actions/`)
is already a separate package; the "brain" *name* is overloaded by the Cedric
action-plane connect flow (`/dashboard/connections/brain/*`), so all new
data-plane routes stay under `/org/knowledge/`.

Rejected: a separate service (premature — one App Runner service, one DB, one
worker today; the AWS doc stages the split), and a new top-level package
(duplicates the gate/worker/router plumbing for no isolation gain).

### D2 — Source-agnostic connector contract with an injectable transport
`knowledge/connectors/base.py` defines typed records (`RemoteItem`,
`RemotePermission`, `RemotePrincipal`, `DeltaPage`) and the `SourceConnector`
protocol: `list_resources`, `delta(resource_key, checkpoint)` (checkpoint `""`
= initial crawl; the same call shape serves crawl and incremental sync),
`fetch_content`, `fetch_permissions`, `fetch_principals`. Typed exceptions
(`ConnectorThrottled(retry_after)`, `ConnectorAuthRevoked`,
`ConnectorItemGone`) carry control flow the sync engine understands for any
source. The Microsoft Graph adapter (`connectors/msgraph.py`) takes a
**transport object** in its constructor; `connectors/fake_graph.py` is a
fully synthetic in-memory Graph tenant (drives, driveItems, delta pages with
`@odata.nextLink`/`@odata.deltaLink`, permissions with inheritance, users,
groups with transitive membership, programmable 429s and 401-revocation) so
the whole slice runs key-free. The real HTTP transport exists as a thin,
deliberately unexercised skeleton (client-credentials mint + Retry-After
handling); it is never called by tests or the demo.

Checkpoints are **opaque strings** — for Graph they are literally the
`nextLink`/`deltaLink` URLs, saved transactionally *after* each page is
applied, which makes crawls resumable mid-way and replay idempotent
(re-applying a page upserts by external ID + version and is a no-op).

### D3 — Normalized ACLs, default deny, staleness ⇒ deny
New tables (migration `0011`): `knowledge_principals` (user / group / tenant /
domain / everyone / link principals, scoped per connection, preserving source
IDs), `knowledge_group_edges` (direct membership; expanded recursively with a
depth cap at query time), `knowledge_document_acl` (per-document grants with
`inherited_from` lineage; chunks inherit through `document_id`, so ACL lineage
survives chunking), `knowledge_identity_map` (Laura caller → source
principal), and `acl_synced_at`/`acl_error` stamps on documents. The rules:

- A document with **no** ACL rows is visible to **no one**.
- A caller with **no** identity mapping resolves to an **empty** principal
  set and retrieves **nothing**.
- ACL older than `knowledge_acl_stale_seconds` ⇒ the document is filtered
  out at query time (stale = deny, surfaced on the status page rather than
  answered around).
- Permission fetch is part of delta processing (permission-only changes
  update ACL rows without re-ingesting content, detected by unchanged source
  version).
- Revoked or non-active connections are excluded by a query-time predicate —
  content stops being retrievable the moment status flips, before any purge.

### D4 — Query-time filtering in SQL, then hybrid fusion
Retrieval (`knowledge/retrieval.py`) builds its candidate set in **one SQL
query** whose predicates are: RLS org pin + explicit `org_id`, source active
+ connection active, document published + not tombstoned, ACL fresh, and
`EXISTS` against the caller's expanded principal set. Only then are
candidates scored: Postgres FTS rank (lexical) fused with cosine over
per-chunk stored embeddings (semantic; key-free `hash` provider by default)
via reciprocal-rank fusion plus a recency boost. Excerpts return with full
citation lineage (title, source, web URL, timestamp, document/chunk IDs).
Nothing — not a snippet, count, or title — crosses the boundary before the
ACL filter. The per-(org,avatar) prebuilt-index design of M1 cannot express
per-user visibility (one index per visibility set does not scale), which is
why M2 filters a DB-resident index at query time instead.

Rejected for the slice, staged in `AWS-DEPLOYMENT.md`: pgvector / OpenSearch
(the slice's Python-side cosine over a SQL-filtered candidate pool is correct
and adequate below ~10⁴ chunks/tenant; the swap point is isolated inside
`retrieval.py`).

### D5 — Sync engine on the existing job queue; webhooks accelerate only
One new job kind, `connector_sync`, on `knowledge_sync_jobs` (claim/lease,
SKIP LOCKED, retry ladder). A run: directory sync (principals + group edges)
→ per-resource delta pages under a per-run page budget → per item: tombstone,
or version-compare → content fetch + re-chunk + re-embed only when changed →
permission fetch + ACL replace → checkpoint save. Throttling reschedules the
job honoring `Retry-After` (not the failure ladder); revocation flips the
connection status and stops. A webhook endpoint validates a shared
`clientState` secret and merely **enqueues** a sync — correctness never
depends on webhook delivery or payload content.

### D6 — Read-only tool; retrieved text is data
The OpenClaw/meeting tool `company_brain_search` is a pure-read function in
`brain/tools.py`'s `_DISPATCH`: un-prefixed (structurally unreachable by the
Cedric MCP action path), performs no writes, registers no actions, and its
results are wrapped in explicit untrusted-content framing before they re-enter
any model context. Authorization is enforced **at dispatch time** (not only
at spec-assembly time): the tool re-checks the feature gate and resolves the
audience itself. Meeting sessions have no bound human user today, so the
tool's default audience is `none` (retrieves nothing) unless the org opts
into `org-public` audience (tenant-wide-shared documents only) via
`knowledge_meeting_audience` — least privilege by default.

### D7 — Key-free everything
Fake Graph transport + `hash` embeddings + embedded Postgres (pgserver) with
a real `laura_app` NOSUPERUSER/NOBYPASSRLS role and real `alembic upgrade
head`. The 14-scenario acceptance suite runs with zero API keys and proves
tenant isolation against actual RLS, not mocks.

### D8 — What v1 deliberately does not do
No customer-managed keys; no operator break-glass; no SCIM; no
sensitivity-label decryption (labeled items are skipped and surfaced); no
real Graph HTTP calls; meeting-agent per-user binding (organizer → principal)
is designed but not wired; hard purge is an admin-credential runbook, not a
runtime capability (runtime tombstones + derived-data deletion only, matching
the 0010 grant philosophy).

## Consequences

- The retrieval core never changes per connector; a new source is an adapter
  plus fixtures plus the same 14-test matrix (`CONNECTOR-ROADMAP.md`).
- Everything inherits org isolation twice (RLS + explicit predicates) and the
  existing ops posture (single worker loop, epoch convergence) — scaling
  beyond one instance/tenant-size tier is an infra change (`AWS-DEPLOYMENT.md`),
  not a schema change.
- The key-free demo, the live-meeting contract routes in `main.py`, and the
  frozen v1 surfaces (Surface API v1, X-Laura-Signature, pinned Anam SDK) are
  untouched.
