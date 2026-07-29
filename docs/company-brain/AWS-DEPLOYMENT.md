# Company Brain — AWS deployment proposal

Constraint: compatible with Laura's current footprint (FastAPI on App Runner
eu-central-1, 1 vCPU/2 GB, Postgres control plane, S3-or-disk knowledge
storage, in-process worker loop, ~$15/mo baseline). The proposal is staged so
each tier is a deliberate upgrade, not a rewrite — the vertical slice's seams
(storage.put_bytes, datastore.retrieval_candidates, the transport factory,
the job queue) are exactly where the tiers swap in.

## Tier 0 — pilot (what the slice runs on, hardened)

One App Runner service, one Postgres (RLS control plane), one region.

- **Object/raw content:** S3 bucket (`KNOWLEDGE_BUCKET`), SSE-S3 encryption,
  bucket policy restricted to the App Runner instance role, prefix
  `knowledge/{org_id}/{document_id}/`, versioning ON, lifecycle rule to
  `INTELLIGENT_TIERING`. Already wired (`knowledge/storage.py`).
- **Canonical metadata/ACLs:** the existing Postgres with FORCE-RLS
  `knowledge_*` tables (0010+0011). TLS in transit, encryption at rest via
  the managed Postgres provider; runtime role `laura_app`
  NOSUPERUSER/NOBYPASSRLS; migrations only through the admin URL.
- **Search/vector:** Postgres FTS + per-chunk `embedding_json` cosine over an
  SQL-filtered candidate pool (in the app). Correct and adequate to ~10⁴
  chunks/tenant; this is the first thing Tier 1 replaces.
- **Sync job state:** `knowledge_sync_jobs` + `knowledge_sync_state`
  (claim/lease, SKIP LOCKED) inside the same Postgres — no new infra.
- **Secrets:** Graph client secret + per-connection webhook clientState in
  **SSM Parameter Store SecureString** (the repo already does this for
  Cedric per-org secrets — reuse `secret_registry`'s pattern); OAuth client
  config in App Runner env from SSM. Nothing in the DB, nothing in git,
  nothing in indexed metadata (tested).
- **Audit:** `knowledge_audit` table (INSERT+SELECT only) — syncs, queries,
  denials, tombstones, revocations. Ship to CloudWatch Logs via a nightly
  export job when SIEM export is asked for.
- **Webhooks:** App Runner is already public — `/webhooks/knowledge/graph`
  terminates Graph change notifications; enqueue-only, clientState-validated.

Cost: +$1–3/mo (S3 + SSM). No new services.

## Tier 1 — first paying tenants (10⁵–10⁶ chunks, real embeddings)

- **pgvector** on the Postgres (or Aurora PostgreSQL Serverless v2 if the
  managed provider lacks it): `knowledge_chunks.embedding vector(512)`,
  HNSW index, and the candidate SQL gains `ORDER BY embedding <=> :qvec`
  *inside the same ACL-filtered query* — the tenant/ACL predicates stay
  exactly where they are (this is why the slice kept filtering in SQL).
  `retrieval.py` swaps its Python cosine for the SQL operator; nothing else
  moves.
- **Dedicated worker service:** the sync loop moves from the API lifespan to
  a second App Runner service (same image, `WORKER_MODE=1` entrypoint) so a
  large tenant's backfill can never starve the live meeting path (recon
  gap: both loops currently share one threadpool). The claim/lease queue
  already tolerates N workers.
- **KMS:** S3 switches to SSE-KMS with a Laura-owned CMK; Fernet secrets in
  SSM already sit on KMS. Per-tenant envelope keys (a data key per org,
  wrapped by the CMK, key id stamped on `knowledge_sources`) when the first
  enterprise security review demands it.
- **Observability:** CloudWatch metrics emitted from the worker per tick —
  sync lag (now − last_sync_at per source), pages/min, throttle count,
  parked jobs, ACL staleness (oldest published acl_synced_at), retrieval
  p95 and result counts. Alarms: lag > 6h, parked > 0, stale-ACL > bound,
  webhook 4xx spike.
- **Backpressure/cost controls:** per-connection page budget (exists),
  per-org daily byte budget on content fetch, embedding batch budget; the
  circuit is "reschedule, don't drop", so budgets change latency, never
  correctness.

Cost: +$30–80/mo (worker service + Aurora floor if adopted).

## Tier 2 — scale/enterprise posture

- **OpenSearch Serverless** (or continued pgvector, decided by tenant size
  distribution) for hybrid BM25+kNN with document-level security filters
  built from the same principal sets; Postgres stays the source of truth,
  the index is derived and rebuildable.
- **SQS between delta detection and item processing** when per-item work
  (OCR, large Office extraction) outgrows the in-DB queue; the in-DB queue
  remains the per-connection scheduler.
- **Extraction sandbox:** move extraction to a Lambda with no egress, small
  tmp, hard timeout — the TB4 boundary gets an actual process boundary.
- **Per-tenant CMKs**, CloudTrail data events on the knowledge bucket, and
  audit export to the customer's SIEM.
- **Region strategy:** bucket + DB stay in eu-central-1 (matches current
  deploy and EU-data expectations); US tenants get a second stack, not
  cross-region replication.

## Retention, deletion, revocation, purge

- **Tombstone path (runtime, exists):** remote delete / revocation ⇒
  unretrievable immediately (tested), rows tombstoned, chunks+ACL deleted.
- **Purge path (admin runbook, deliberate):** a scheduled admin-credentialed
  job hard-deletes tombstoned documents' versions + S3 objects after
  `orgs.retention_days`, and a **tenant purge** runbook deletes the org's
  S3 prefix + relies on `ON DELETE CASCADE` from `orgs` for every
  `knowledge_*` row. Runtime role deliberately cannot do this (no DELETE
  grant on documents/versions).
- **Connector revocation:** flips status (tested) ⇒ retrieval stops at query
  time; purge follows the retention schedule unless the admin requests
  immediate purge.
- **Legal deletion:** per-document admin endpoint = tombstone now + priority
  purge job; audit row retained (append-only) as the deletion record.

## Deploy-order requirement

`0011` adds `knowledge_chunks.embedding_json`, which the shared
`publish_version` path now writes — so **`alembic upgrade head` must run
before the M2 image serves a flag-on org** (standard migrate-before-deploy;
key-free/flag-off deployments are unaffected because the whole plane 404s).
Make the migration a release/pre-traffic step, not a container-boot step
(booting must not depend on DB availability).

## Explicit non-goals at every tier

No cross-region replication of customer content, no operator read path into
tenant indexes, no customer-visible raw-token storage anywhere, and the
key-free demo never touches any of this (flag off ⇒ 404, tested).
