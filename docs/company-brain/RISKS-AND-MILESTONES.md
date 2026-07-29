# Company Brain — unresolved risks & the next production milestone

Honesty gate first: the slice proves ACL synchronization, deletion
propagation, tenant isolation and retrieval authorization **end to end
against a synthetic Graph tenant and embedded Postgres** (32 passing tests,
zero keys). That is the required evidence bar for the *design* — it is NOT a
claim of enterprise readiness. The gaps below are what stand between the
slice and a real tenant.

## Adversarial verification pass (what the red team found and what changed)

A five-lens adversarial review (attacking tenant isolation, ACL default-deny,
injection, contract preservation, and the Graph permission matrix) ran against
the code on real Postgres. **Strict cross-tenant isolation held under every
attack** (composite `(org_id, id)` FKs make a cross-org reference physically
unstorable; a leaked foreign principal id fed into the candidate query returns
nothing). But it found real defects, since **fixed and regression-tested**:

- **Critical — connector ACL bypass:** the M1 avatar-assignment bridge
  (`chunks_for_avatar`, feeding the live-meeting index) and `keyword_search`
  had no ACL predicate, so assigning an msgraph source to an avatar would
  serve permissioned documents to anyone. Fixed: both queries are restricted
  to `kind IN ('upload','drive')` and `dal.assign` refuses connector sources —
  connector content is retrievable **only** through the ACL-filtered
  `retrieval.query` path. (`test_connector_sources_cannot_be_assigned_to_an_avatar`.)
- **High — HTTP surface 500 on Postgres ≥14:** every `extract(epoch …)` returns
  `Decimal`, which the routes' raw `JSONResponse` can't serialize (the
  in-process tests never crossed it). Fixed: `::float8` casts + a real
  TestClient-over-Postgres test. (`test_http_surface_serializes_and_gate_is_strict`.)
- **High — machine-gate fail-open:** `/org/knowledge/*` fell back to the demo
  org for an unauthenticated caller. Fixed: the data-plane gate now returns 401
  and never falls open. (Same test.)
- **High — injection via metadata fields:** only the excerpt was
  delimiter-neutralized; a document *titled* `</company-brain-document>` could
  forge a closer. Fixed: title/source/url/section are neutralized too.
  (`test_format_for_model_neutralizes_injection_via_metadata_fields`.)
- **Medium — failed ACL refresh served stale-permission content:** added
  `AND d.acl_error = ''` so a document whose permission sync just failed is
  denied immediately. (`test_acl_fetch_failure_denies_new_content`.)
- **Medium — org-public served without opt-in / webhook 500 on malformed
  bodies:** the `org-public` audience now honors the config gate on both call
  sites, and the webhook validates shapes + compares bytes (no non-ASCII 500,
  no existence oracle).
- **Doc (four, verified against live learn.microsoft.com):** the Graph
  permission matrix was corrected — OneDrive `Files.SelectedOperations.Selected`
  is GA; mail scoping is RBAC-for-Applications not the legacy access policy;
  Teams metering was retired 2025-08-25; `.default` admin consent grants all
  registered permissions (so per-tier consent needs separate registrations).

## Unresolved risks (ranked)

1. **Identity linking is manual.** `knowledge_identity_map` is populated by
   an admin API call; production needs OIDC-based linking (Entra sign-in →
   object id) or admin bulk-mapping by verified email. Until then, "the user
   asking" is only as trustworthy as the mapping process. Related: Google-
   login users in an M365 org have no automatic Entra identity.
2. **Machine-door user assertion.** An org bearer may assert `user_email`;
   it is validated against the org's identity map (and a browser session can
   never assert anyone else), but a stolen org bearer can query as any
   *mapped* user of that org. Mitigations staged: scoped machine tokens
   (read-only, per-audience), short expiry, and audit alerting on unusual
   assertion patterns. Not built.
3. **Real Graph behavior is unexercised.** The HTTP transport skeleton has
   never met a real tenant: delta-token expiry (410 `resyncRequired`),
   permission pagination, `siteUser` id quirks, protected-API approval for
   Teams, subscription lifecycle renewals. The fake encodes our *model* of
   Graph; a synthetic-tenant pilot must validate the model.
4. **Directory sync is full-refresh per run.** Users/groups re-upsert and
   edges wholesale-replace on every sync — correct but O(directory) per run;
   large tenants need Graph delta on `/users` and `/groups` and
   memberships-changed handling. Cost, not correctness.
5. **Semantic tier is slice-grade.** Hash embeddings + Python cosine over an
   SQL-filtered pool; quality and scale both need pgvector + a real
   embedding provider (Tier 1 in AWS-DEPLOYMENT.md). Score calibration
   (grounding floors) must be redone when the provider changes.
6. **Worker shares the API process.** A backfill competes with the live
   meeting path for the threadpool; the split is a deploy change (Tier 1),
   but until then large initial crawls need the page budget kept small.
7. **Sensitivity labels (MIP) are unhandled.** Label-encrypted files will
   fail extraction and quarantine — surfaced, not decrypted — but the status
   page should name them explicitly so admins aren't debugging "failed".
8. **Purge is a runbook, not code.** Tombstone-to-hard-delete and full
   tenant purge are designed (AWS doc) but there is no scheduled job yet;
   retention promises can't be made to customers until it exists and is
   tested.
9. **Audit has no reader.** Events are written (and tested) but there is no
   dashboard/status-page surface for queries/denials/lag yet; enterprise
   buyers will ask to see it.
10. **Repo reality.** This repo is the archived SFF fork; prod is upstream
    `Dunic15/Laura`. Landing this work requires the same porting motion as
    the restructure branch (docs/BACKEND-RESTRUCTURE.md:8-10).

## Smallest next production milestone (M3: "one real synthetic tenant")

Scope — nothing else:

1. Register the multi-tenant Entra app (tier A + `Sites.Selected` only);
   publisher verification started.
2. Stand up a Microsoft 365 developer tenant **we own** with synthetic
   content mirroring the fake fixture (nested groups, inherited folder ACLs,
   an org-wide doc, a private doc).
3. Wire `GraphHttpTransport` behind the existing transport factory with
   secrets in SSM; admin-consent + site-picker flow for that one tenant.
4. Run the SAME 14-scenario suite against the real tenant (a `graph_live`
   pytest marker, skipped by default, never in CI) — the fake and the live
   run must agree; every disagreement is a bug in the fake to fix.
5. Delta-token expiry handling (410 → full re-crawl from `""`, which the
   engine already supports) — the one known-real behavior the fake doesn't
   force.
6. Connector status card in the dashboard reading `source_status` (data is
   already served).

Exit criteria: 14/14 live-tenant scenarios green twice (fresh crawl and
after a week of drift), throttling observed and survived, revocation drill
performed from the Entra portal, zero secrets outside SSM. Only after that
does "pilot with a design-partner tenant" become a conversation.
