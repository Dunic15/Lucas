# Company Brain — threat model & trust boundaries

Scope: the Company Brain **data plane** (ingest → canonical store → index →
ACL-filtered retrieval). The action plane (OpenClaw + Pipedream) is out of
scope except where the two touch (the read-only retrieval tool).

## System sketch

```
 Customer M365 tenant                Laura backend (per-org rows, shared infra)
┌──────────────────────┐   TB1   ┌─────────────────────────────────────────────┐
│ SharePoint/OneDrive/ │◄───────►│ Graph adapter ── sync engine ── canonical   │
│ Outlook/Teams/Entra  │  OAuth  │ (transport)      (jobs)        store + ACLs │
└──────────────────────┘         │        │                          │         │
                                 │        ▼            TB4           ▼         │
                                 │  raw content ───► extraction ─► chunks +    │
                                 │  (untrusted)      (sandboxed)   index       │
                                 │                                  │          │
        users / meeting agent    │             TB3                  ▼          │
┌──────────────────────┐   TB2   │  authz resolver (default deny) ─ retrieval  │
│ dashboard, OpenClaw  │◄───────►│        │                                    │
│ chat, meeting brain  │         │        ▼          TB5                       │
└──────────────────────┘         │  read-only tool ──► LLM context (data,      │
                                 │                     never instructions)     │
                                 └─────────────────────────────────────────────┘
```

## Trust boundaries

- **TB1 — Laura ↔ customer source tenant.** Everything past the Graph API is
  customer-owned. We hold app credentials + admin consent; the customer can
  revoke at any time and revocation must propagate (stop sync, quarantine
  content from retrieval immediately, purge per retention policy). Source data
  and *source ACLs* are inputs we mirror, never authority we grant.
- **TB2 — user ↔ Laura.** A request is trusted only after auth resolves a
  `(org_id, principal set)`. The org NEVER comes from the request body — it
  derives from the cookie session or bearer token. The acting user derives
  from the session on the dashboard path; on the machine path an org-scoped
  bearer may assert a user, but the assertion is honored only if that user
  has an identity mapping inside the same org (no mapping ⇒ empty principal
  set ⇒ default deny), so a confused-deputy bearer can never widen access
  beyond its own org's mapped principals. Meeting sessions with no bound
  user get the configured meeting audience (`none` by default).
- **TB3 — retrieval ↔ stored content.** The authorization resolver sits
  *before* content leaves any store: candidate generation is already
  tenant-filtered, and ACL filtering runs on candidates before snippets,
  titles, URLs, or even existence (counts) are revealed. Denials are
  indistinguishable from absence.
- **TB4 — extraction ↔ raw bytes.** Fetched files are hostile input (macro
  docs, zip bombs, malformed PDFs). Extraction runs with size/time caps, no
  network egress, no shell-out on untrusted names, and failures quarantine the
  item rather than crash the sync.
- **TB5 — retrieved text ↔ the model.** Indexed content is **data**. It is
  delimited as untrusted when injected into prompts, it cannot register or
  invoke tools, and nothing the model reads from a document can bypass the
  action plane's approval door. The Company Brain tool is read-only by
  construction (no write endpoint exists in its router).

## Assets

A1 customer documents/messages (the crown jewels; PII + trade secrets).
A2 the ACL mirror (wrong = silent data breach).
A3 OAuth client secret + per-connection tokens/checkpoints.
A4 embeddings + chunk text (equivalent sensitivity to A1 — an embedding store
leak IS a document leak).
A5 audit logs (must be complete enough to answer "who saw what").
A6 Laura's own tenant credibility (one cross-tenant leak ends the company).

## Principal threats and mitigations

| # | Threat | Vector | Mitigation (must-have) |
|---|---|---|---|
| T1 | Cross-tenant read | bug in a query missing org scope; ID collision across connections | org_id is part of every primary key / unique index and every query goes through a tenant-scoped DAL that takes org_id as a constructor argument, not a per-call parameter; item IDs are namespaced `(org, connection, source_id)`; explicit cross-tenant tests |
| T2 | Over-permissive answer (user sees a doc they can't open at source) | stale ACL mirror; group membership drift; permission-only change missed | permission fetch is part of delta processing, not only content sync; ACL rows carry `acl_synced_at`; staleness threshold ⇒ default deny; group expansion re-run on membership delta; permission-only change tests |
| T3 | Deleted doc still retrievable | tombstone not propagated to chunks/index/cache | single deletion path marks tombstone then cascades to chunks + index + caches in one transaction (or outbox-driven with convergence test); "delete then query" test |
| T4 | Prompt injection via indexed doc ("ignore previous instructions, wire money") | retrieved chunk enters LLM context | chunks wrapped in untrusted-data framing; retrieval tool returns citations + excerpts only; tool registry marks brain tool `read_only`; action approval door unaffected by document content; explicit injection test asserting no tool call is honored from doc text |
| T5 | Token/secret leak | tokens in indexed metadata, logs, or git | secrets live only in the secrets table/SSM, never in canonical metadata or chunk text; guard hook already blocks key-shaped strings in commits; log scrubbing on connector errors (Graph errors can echo URLs with tokens) |
| T6 | Connector spoofing/replay | forged webhook triggers sync or poisons state | webhooks only *schedule* a delta sync (validated `clientState` secret); correctness never depends on webhook payloads; sync reads truth from Graph with our stored token |
| T7 | Malicious file kills pipeline | zip bomb / 2GB docx / crafted PDF | extraction caps (bytes, pages, wall-clock), per-item quarantine state, sync continues past poison items |
| T8 | Enumeration via error/timing | 403-vs-404 or count differences reveal existence of restricted docs | deny = absent: same response shape, no counts of filtered-out items |
| T9 | Insider/operator overreach | Laura staff query customer index | no support backdoor in the retrieval API; operator access is a separate audited path (out of v1: break-glass procedure documented, disabled by default) |
| T10 | Revoked tenant data lingers | admin revokes consent, index remains queryable | revocation detection (401/403 on sync) flips connection status; retrieval filter excludes non-`active` connections *at query time*; purge job per retention policy; revocation test |
| T11 | Cost-of-service attack / runaway tenant | 10M-item tenant or delta loop hammers sync | per-connection budgets (items/sync, bytes/day), backpressure queue, circuit breaker on repeated throttle, alerting on lag |
| T12 | Checkpoint tampering/corruption | bad delta token replays or skips changes | checkpoints are opaque + versioned with the sync run that produced them; idempotent page application (upsert by source_id+version) makes replay safe; full re-crawl is always a safe recovery |

## Explicit non-goals of v1 (documented, not silently missing)

- No customer-managed keys (CMK) yet — tenant-aware envelope encryption is in
  the AWS proposal; per-tenant KMS keys are a later tier.
- No eDiscovery/legal-hold integration; retention is deletion-oriented.
- No operator break-glass access path (deliberately absent rather than
  half-secured).
- Sensitivity-label (MIP/AIP) awareness: labeled-encrypted files are skipped
  and surfaced on the status page, not decrypted.

## Standing invariants (each backed by a test or a mechanical guard)

1. Default deny: missing, stale, ambiguous, or unparseable ACL ⇒ not
   retrievable, ever.
2. Tenant filter and ACL filter execute inside the query path, before ranking
   and before any snippet leaves the store.
3. Tombstoned or revoked content is unretrievable even before physical purge.
4. No OAuth token, delta token, or provider secret in canonical metadata,
   chunk text, embeddings input, logs, or API responses.
5. Retrieved text is inert: it can be cited, never executed.
