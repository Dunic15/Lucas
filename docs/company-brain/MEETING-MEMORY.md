# Meeting Memory & Pre-Meeting Context (M3)

Branch: `claude/company-brain-meeting-memory` (base `7e085dd`, the integrated
OpenClaw head). Local only — nothing pushed, deployed, or pointed at AWS.

## Why this shape

The Company Brain M2 work was built on the archived fork, where no equivalent
subsystem existed. On this base there **is** one: the Data Foundation (DF,
accepted contract v5) already owns the canonical record model, immutable
versions, the fail-closed ACL mirror, identity/group closure, quarantine,
retention, and — explicitly — the *single* retrieval boundary
(`resolver.py`: "This module does not create a second retrieval subsystem").

So M2/M3 were ported **semantically, not literally**: everything that DF
already does is used rather than duplicated. What the fork's Company Brain
contributed is the part DF lacked — a real external connector (Microsoft
Graph), a native meeting source, meeting-shaped retrieval with filters, the
pre-meeting brief, and untrusted-content framing.

| Fork M2 concept | Where it lives now |
|---|---|
| `SourceConnector` contract | DF `connectors.get(kind).sync() -> SyncBatch` |
| Graph adapter + fake transport | `datafoundation/connector_msgraph.py`, `fake_graph.py` |
| Principals / groups / ACL rows | DF `df_identities`, `df_identity_edges`, `df_record_acl` (unchanged) |
| Delta checkpoints | DF `df_connector_cursors` (advanced atomically with the batch) |
| Tombstones / revocation | DF envelope `deleted` + connector status (unchanged) |
| ACL-filtered candidate SQL | DF `dal.visible_heads` (unchanged) |
| Hybrid retrieval | DF `resolver.resolve` + `meeting_memory.search` |
| Untrusted-content framing | `datafoundation/framing.py` (new — DF had none) |
| Pre-meeting brief | `datafoundation/premeeting_context.py` (new) |

## Data flow

**Ingest — Microsoft Graph.** `sync.process_due` claims a run, calls
`MSGraphConnector.sync`, which walks `/root/delta` per drive. Each item
becomes a SourceEnvelope; each item's `/permissions` becomes `acl_mode` +
entries: an organization link ⇒ `org_default`, user/group grants (including
those inherited from parent folders) ⇒ `mirrored`, anything unreadable or
unrecognized ⇒ `unknown` (zero ACL rows: nobody). `dal.commit_batch` applies
envelopes, identities, group edges and the new cursor in ONE transaction.
410 `resyncRequired` ⇒ full reconciliation; 429 ⇒ bounded `Retry-After`
backoff, then the run's own retry ladder; 401/403 ⇒ `ScopeLostError`, which
drops the connector out of eligibility so its mirrored records stop being
visible immediately.

**Ingest — meetings.** At the tail of `lifecycle.finalize` — after the
artifact is saved and the meter stopped — `connector_meeting.emit_finalized`
distills the artifact into one envelope: title, date, platform, source
reference, participants, customer/project/topics, summary, decisions,
actions (owner + due), risks, open questions. **The transcript is never
included.** Artifact visibility maps to ACL: `org` ⇒ `org_default`,
`participants` ⇒ `mirrored` over attendee identities, `private` ⇒ `mirrored`
over the dispatcher; if no attendee resolves to a stable identity the record
becomes `unknown` — visible to nobody rather than to everybody. The hook is
best-effort: memory can never fail a finalize.

**Retrieval.** `meeting_memory.search` runs in a fixed order that cannot be
reordered without failing tests: (1) `dal.visible_heads` for the
authenticated principal — DF's single visibility query; (2) facet filters
(participant / customer / project / topic / date range) which can only
narrow; (3) text scoring + recency. No principal ⇒ org-visible only, never
everything. Citations carry meeting title, date and meeting id.

**Pre-meeting context.** `premeeting_context.build(org, principal, event)`
composes meeting history (via `meeting_memory.search`) with company
documents (via the DF `ContextResolver`), harvests open commitments and
risks out of the distilled sections, and returns a bounded, cited pack:
relationship/account context, what happened previously, open commitments,
company knowledge, risks and open questions, suggested discussion points,
citations, freshness stamp. Read-only end to end: no writes, no executor, no
Pipedream, no queued action. A suggestion stays a suggestion — turning one
into work remains the Action Center's job behind explicit approval.

## Surfaces

| Route | Auth | Principal |
|---|---|---|
| `POST /org/data/meetings/search` | org machine bearer | none ⇒ org-visible only |
| `POST /org/data/premeeting` | org machine bearer | none ⇒ org-visible only |
| `GET /dashboard/data/meetings/search` | login cookie | the signed-in user |
| `GET /dashboard/data/premeeting` | login cookie | the signed-in user |

The dashboard routes are GET because they are strictly reads a normal
attendee must be able to make; the twin's admin gate covers mutations. The
machine routes follow the existing `/org/data/resolve` precedent exactly: a
machine bearer carries org authority, not a human identity, so it never sees
principal-scoped records. Per-user access from the meeting avatar needs its
session to carry a bound principal — see Gaps.

## Schema

- `0024_df_msgraph_meeting_kinds` — widens the `df_connectors` kind CHECK to
  admit `msgraph` and `meeting` (additive superset; existing rows stay valid).
- `0025_meeting_memory_facets` — `df_meeting_facets`, a derived projection
  (customer, project, topics, participants, series key, occurred_at) with
  FORCE RLS and least grants. **Not an access surface**: every query joins it
  against the ACL-filtered head set, so a facet can only narrow. Replaced
  wholesale on re-index; cascades with the record.

## Guarantees (each backed by a test)

1. An inaccessible meeting contributes no snippet, no citation, no count.
2. Missing identity, unresolvable attendees, revoked connector, or a
   tombstone ⇒ nothing retrievable (default deny, all four).
3. Cross-org access is impossible — composite `(org_id, id)` FKs plus RLS.
4. The raw transcript is never indexed and never retrievable.
5. Re-finalizing the same meeting is idempotent (one record, one facet row).
6. Retrieved text is inert: the framing delimiter is neutralized in body,
   title, source, url and section, so content cannot escape its block.
7. Building a context pack executes no external action.

## Gaps / next

- **Per-user avatar access.** The meeting avatar's session has no bound
  principal, so the avatar path today gets org-visible evidence only. Binding
  a session to a principal is the unlock.
- **Attendee identity.** Recall rosters are display names; an attendee
  becomes an ACL subject only when an email arrives (calendar/orchestrator).
  Without one the meeting is org-visible or nobody-visible by policy.
- **Semantic recall for mirrored records.** DF's embedding index is
  org_default-only by construction, so principal-scoped meetings are keyword-
  ranked. Improving that belongs *inside* `resolver.py`.
- **No scheduler.** DF runs sync only when someone enqueues; a periodic
  enqueue is needed before a real Graph tenant.
- **Retention link.** Artifact expiry does not yet tombstone its meeting
  record; Meeting Memory needs its own retention pass.
