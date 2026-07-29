# Company Brain — staged connector roadmap

Microsoft Graph is connector #1 (built in this repo, fake-transport slice
first). This document stages the next five sources against the same
source-agnostic contract (`SourceConnector`: initial crawl → incremental sync →
item fetch → permission fetch → deletion detection → revocation). **No new
connector may touch the retrieval core** — if a source can't be expressed in
the canonical document + ACL model, the model gets a review, not a bypass.

Ordering rationale: (1) how often the ICP (ops-heavy 50–500p companies) keeps
process docs there, (2) delta/ACL API maturity (cheap correctness), (3) sales
motion (which connector unblocks the next pilot).

## Stage 1 — Google Drive (first fast-follow)

The mirror image of the OneDrive/SharePoint path; highest ICP overlap for
Google-Workspace shops (Laura already has Google OAuth + a Drive client to
build on).

- **Delta:** `changes.list` with page tokens — true durable cursor, includes
  removals (`removed=true`) and drive-scoped changes. Maps 1:1 onto our
  checkpoint contract.
- **Content:** `files.get` / `files.export` (Docs/Sheets/Slides → text export).
- **ACLs:** `permissions.list` per file — user / group / domain / `anyone`
  types map directly onto our principal model. Inherited permissions on shared
  drives come from the drive; on My Drive from parent folders (must be
  materialized, Drive API does not expand them per-file reliably).
- **Groups:** Google Groups membership via Directory API (Workspace admin
  consent) — same group-expansion pipeline as Entra groups.
- **Scoping:** per-shared-drive / per-folder allowlist (ours, enforced in the
  connector) + domain-wide delegation or per-user OAuth.
- **Risks:** domain-wide delegation is the enterprise-scary Google equivalent
  of tenant-wide consent — offer marked-folder scoping first. Export API quota
  on large Docs corpora.

## Stage 2 — Slack

Highest message-volume source; valuable for "what did we decide" questions but
noisy — ship after the document sources prove retrieval quality.

- **Delta:** no true delta API — cursor = per-channel `conversations.history`
  with `oldest` timestamp watermarks + Events API (`message`, `message_changed`,
  `message_deleted`, `channel_*`) as accelerators. Periodic reconciliation
  crawl required (our webhook-optional design already assumes this).
- **ACLs:** channel membership *is* the ACL — private channel → members only;
  public channel → all non-guest workspace members; DMs/MPIMs → participants
  only (recommend excluding DMs in v1, same reasoning as Teams chats).
  `conversations.members` + user groups via `usergroups.*`.
- **Tombstones:** `message_deleted` events + reconciliation diffing; Slack
  message edits create version churn — dedup by `(channel, ts, edited_ts)`.
- **Chunking:** thread = document, message = section; never chunk across
  threads.
- **Scoping:** channel allowlist chosen by admin at install (Slack app scopes:
  `channels:history`, `groups:history` only if private channels opted in,
  `users:read`, `usergroups:read`).
- **Risks:** Enterprise Grid orgs need org-wide app + per-workspace grants;
  rate limits (Tier 3) make initial crawl of large workspaces slow — needs the
  backpressure/budget controls from day one. Marketplace review if distributed.

## Stage 3 — Notion

Common wiki for the ICP's smaller half; API is the least sync-friendly of the
set.

- **Delta:** no delta API, no deletion events — poll `search` +
  `last_edited_time` filter as watermark; deletion detection **requires**
  periodic full-ID sweeps (list all pages, diff against index → tombstones).
  This is the most expensive correctness loop of the six sources; budget it.
- **ACLs:** integration sees only pages explicitly shared with it — which is a
  *feature* for scoping (admin shares the spaces to index) but page-level
  user/group ACLs are not fully readable via API → conservative model: treat
  every indexed page as visible only to the workspace members the admin maps
  at connect time, or integrate SCIM (Enterprise plan) for group data.
  **Default deny stands:** if Notion can't tell us who may read a page, the
  page inherits the connection-level audience the admin explicitly configured,
  never "everyone".
- **Chunking:** block tree → sections (headings/toggles); databases → one doc
  per row with property table as structured section.
- **Risks:** ACL fidelity is the weakest of all six — must be documented to
  the customer as "space-level, admin-scoped" not "per-page mirrored".

## Stage 4 — Confluence

The enterprise wiki incumbent; strong APIs, appears exactly when deal size
justifies it.

- **Delta:** CQL search ordered by `lastmodified` as watermark + webhooks
  (Connect/Forge or on-prem webhooks) as accelerators; audit/deleted content
  via content status + periodic sweep for hard deletes.
- **ACLs:** space permissions + page restrictions (user/group), inherited
  restrictions from ancestors — maps cleanly onto our inherited-ACL model;
  groups via the Atlassian admin APIs.
- **Content:** storage-format XHTML → structured sections; attachments (PDF,
  Office) go through the same extraction pipeline as Graph driveItems.
- **Scoping:** per-space allowlist (native fit).
- **Risks:** Cloud vs Data Center API divergence — target Cloud only in v1;
  anonymous-access spaces must map to `public` principal deliberately, not
  accidentally.

## Stage 5 — Box

Least ICP-frequent; add when a specific enterprise deal requires it.

- **Delta:** `events` stream with durable stream position (true cursor,
  includes deletes) + `folders/{id}/items` reconciliation.
- **ACLs:** collaborations (user/group, role-based) per file/folder with
  inheritance via folder tree; shared links map to link-audience principals
  (`open`/`company`/`collaborators`) — direct fit for our link-scope model.
- **Scoping:** per-folder allowlist; Box app authorization is admin-approved
  in the enterprise console (clean consent story).
- **Risks:** none unusual — Box is the most "boring" adapter; its main cost is
  simply being sixth in line.

## Cross-cutting gates for every new connector

1. Adapter implements the full `SourceConnector` contract including
   `fetch_permissions` and deletion detection — a connector without readable
   ACLs ships behind an explicit "connection-audience only" mode (Notion
   precedent), never with implied tenant-wide visibility.
2. The 14-scenario test matrix (crawl, delta, tombstone, permission-only
   change, inherited ACL, user/group ACL, unauthorized deny, cross-tenant,
   stale-perm deny, throttle retry, idempotent replay, citation correctness,
   injection resistance, revocation) is instantiated against a fake transport
   for that source before any real-credential test.
3. Real-credential pilot runs against a synthetic tenant/workspace we own,
   never a customer's.
4. Connector status page fields (connected, initial-sync progress, last
   successful sync, docs indexed, permission health, errors) must be populated
   by the adapter from day one.
