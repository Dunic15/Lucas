# Meeting Memory Graph — accumulating company brain

Status: **spec accepted; Slice 1 in build** (2026-07-30). Owner intent captured
2026-07-30; owner-ratified decisions marked ✦ below.
Companions: `ARCHITECTURE.md` (D1–D8), `RISKS-AND-MILESTONES.md`, `CONTRACTS.md`,
`THREAT-MODEL.md`, `docs/product/LAURA-COMPANY-BRAIN-SKILLS-BROWSER-ROADMAP.md`.

Everything here is **behind a flag** (`MEETING_MEMORY_ENABLED`, default `false`)
and, for the graph/retrieval half, additionally behind `knowledge.enabled()`
(`COMPANY_BRAIN_ENABLED` **and** a configured control plane). The key-free demo
never touches any of it; the live-meeting contract is untouched.

✦ **Owner-ratified (2026-07-30):**
- Build **Slice 1 only** first; self-audit + code review; then ask the owner
  before each subsequent slice.
- Visibility rule: **all-attendees** + `org-public` opt-in + **admin-grant**
  (an admin can add named people to a specific meeting's access) — see §7.
- Retention: **12 months** full text/embeddings, then cold structured-only
  tier — see §9.

## 1. Problem

The pitch is "an avatar that knows your company". The code today knows **one
meeting link**.

- `ledger.carryover_brief()` (`backend/app/actions/ledger.py:790`) is the entire
  cross-meeting memory: up to **8 open items + the last 3 decisions**, selected
  `WHERE org_id=? AND meeting_key=?`. `meeting_key()` (`ledger.py:56`) is a pure
  URL→platform-code function, so *the weekly standup remembers the weekly
  standup and nothing else*. A decision made in yesterday's customer call is
  invisible in today's internal review.
- The pre-meeting brief (`backend/app/meeting/lifecycle.py:566-610`) already
  composes five other context blocks (Drive folder, Asana snapshot, tool
  registry, calendar, carryover) into `session.memory_brief` — the plumbing for
  "context at join" exists; there is simply **no cross-meeting source** to plug
  into it.
- The finished meeting flows into exactly two places
  (`lifecycle.py:978-990`): the artifact store, and `ledger.record_meeting`
  (open actions / decisions / missing steps, keyed by the same single link). It
  never enters the knowledge plane.
- The knowledge plane (`backend/app/knowledge/*`, migrations `0010`/`0011`) is
  **documents-only**: sources → documents → chunks, with normalized ACLs and
  ACL-filtered hybrid retrieval. It has no notion of a meeting, a person, or a
  project.
- "Graph" in this codebase means **Microsoft Graph** (`connectors/msgraph.py`).
  There is **no knowledge graph**. The building blocks exist as disconnected
  tables: `knowledge_documents`/`knowledge_chunks` (docs),
  `artifacts` (one distilled record per meeting), `session_participants`
  (attendance), `ledger_items` (actions/decisions/missing steps).

**So:** every meeting produces a high-quality distilled artifact, and then that
artifact is a dead end. The accumulation the product promises does not happen.

## 2. Why now, why this size

Three things just became true at once: (a) M2 shipped an **ACL-filtered,
injection-framed, tenant-isolated retrieval path** we can reuse rather than
rebuild — the expensive, dangerous half of this feature is already written and
red-teamed; (b) the artifact is already **distilled** (summary, decisions,
actions, missing steps, readiness, attendees), so meeting memory can be built
**without ever touching a transcript** — the PII constraint is satisfiable by
construction; (c) the pre-meeting brief already exists as a composable slot, so
the first slice is *one new read at join + one new write at finalize*, not a
rewrite.

Competitive read (**assumption, not validated**): the notetaker field
(Fireflies, Granola, Circleback, native Zoom/Meet AI) sells search over an
archive *after* the fact; none of the 2026 buyer's-guide roundups describes a
linked people/meeting/project memory a live agent consults **while the meeting
is happening**. Our differentiation is the *in-meeting* use of accumulated
memory, not the archive itself. Treat as a hypothesis.

## 3. Users and the one "aha"

- **Primary user:** a recurring team (ops/People/IT/RevOps) at a 50–500-person
  company where the same 3–8 people meet across several different meeting links
  each week.
- **The aha (one sentence):** *mid-meeting, someone says "didn't we already
  decide this?" and the avatar answers with the decision, the date, the meeting
  it happened in and who was there — from a different meeting.*
- **Non-goal:** being a search engine over transcripts. We never store them.

## 4. Design principles (inherited, non-negotiable)

1. **Distilled only, never raw.** The graph ingests artifact fields
   (summary/decisions/actions/attendee display names). Transcripts stay in
   memory + the artifact store, never logged (`.claude/hooks/guard.py` enforces
   the logging half mechanically).
2. **Latency is the product.** Nothing new between transcript → first spoken
   token. The join path pays at most a bounded, cancellable cost. The
   every-turn prompt blocks are byte-capped.
3. **Default deny.** No ACL row ⇒ retrievable by nobody (M2 rule D3).
4. **Reuse the plane, don't fork it.** Meeting memory becomes a source *kind*
   inside `backend/app/knowledge/`, so it inherits RLS, the job worker, the
   candidate-filter SQL, untrusted-content framing, tombstones and audit.
5. **Vendor swappability untouched.** No avatar-layer change; a new avatar is
   still a folder.

## 5. Data model

**Decision: Postgres, not a graph database.** Sizing: a 200-person org running
~40 recorded meetings/week produces ~2 000 meetings/year, ~10 entities and ~25
edges each ⇒ ~2·10⁴ nodes and ~5·10⁴ edges per tenant-year — a table that fits
in cache. Our traversals are depth ≤ 3 ("this meeting → attendees → their other
meetings → decisions"), which is a `WITH RECURSIVE` CTE over composite-key
indexes; we already run exactly that shape in
`datastore.principal_ids_for_user` (`backend/app/knowledge/datastore.py:292`).
A second datastore would mean a second tenancy model (Neo4j has no FORCE-RLS
equivalent — isolation would move from the database into our query builder), a
second backup/PITR story, a second migration path, and a second failure mode on
the meeting-join path, all for a graph that fits in RAM. **Revisit only** when a
single tenant exceeds ~10⁶ edges or we need unbounded path queries.

New migration `0012_meeting_memory`, same discipline as `0011`: composite
`(org_id, id)` PK/FK, FORCE RLS + `tenant_isolation` policy, REVOKE-then-GRANT,
CHECK-vocabulary widening via the DROP/re-ADD move.

```
memory_meetings                     -- the meeting entity (one row per bot_id)
  org_id uuid, id uuid, PK (org_id, id)
  bot_id text,  UNIQUE (org_id, bot_id)     -- idempotent re-ingest
  meeting_key text,                          -- ledger.meeting_key(): the SERIES
  avatar_id text, title text, meeting_type text,
  started_at timestamptz, ended_at timestamptz, duration_seconds int,
  readiness_score int,
  summary text,                              -- distilled; NEVER transcript
  decisions_json text, actions_json text, missing_steps_json text,
  document_id uuid NULL,                     -- FK (org_id, document_id) ->
                                             -- knowledge_documents (slice 2)
  created_at timestamptz
  INDEX (org_id, ended_at DESC)              -- the past-week window query
  INDEX (org_id, meeting_key, ended_at DESC) -- the series query

memory_attendees                    -- attendance edge, kept explicit + cheap
  org_id uuid, meeting_id uuid, entity_id uuid,   PK (org_id, meeting_id, entity_id)
  display text, email text, resolution text CHECK (resolution IN
      ('email','directory','display_name')),      -- entity-resolution provenance
  spoke bool
  FOREIGN KEY (org_id, meeting_id) -> memory_meetings(org_id, id) ON DELETE CASCADE
  FOREIGN KEY (org_id, entity_id)  -> memory_entities(org_id, id)

memory_entities                     -- ONE node per real-world thing
  org_id uuid, id uuid, PK (org_id, id)
  kind text CHECK (kind IN ('person','project','document','decision','action','meeting'))
  key  text,                          -- the DEDUPE key (see §6)
  UNIQUE (org_id, kind, key)          -- the anti-duplicate invariant, in the DB
  display text, email text,
  ref_kind text, ref_id text,         -- soft link: knowledge_documents.id |
                                      -- ledger_items.action_id | memory_meetings.id
  alias_of uuid NULL,                 -- merged-away node (§6), never deleted
  first_seen_at, last_seen_at timestamptz, mention_count int
  INDEX (org_id, kind, last_seen_at DESC)

memory_edges                        -- SLICE 2
  org_id uuid, src_id uuid, dst_id uuid,
  rel text CHECK (rel IN ('attended','decided','owns','mentions','about','produced','follows_up')),
  meeting_id uuid,                    -- the meeting that WITNESSED this edge
  confidence real NOT NULL DEFAULT 1.0,
  created_at timestamptz
  PK (org_id, src_id, rel, dst_id, meeting_id)     -- re-ingest is idempotent
  INDEX (org_id, dst_id, rel)
  composite FKs to memory_entities(org_id, id) and memory_meetings(org_id, id)

memory_digests                      -- the CACHED past-week working memory
  org_id uuid, id uuid, PK (org_id, id)
  scope text CHECK (scope IN ('org','avatar')), avatar_id text,
  window_start, window_end timestamptz,
  text text,                          -- <= 1200 chars, hard-capped
  source_meeting_ids_json text, model text, generated_at timestamptz
  UNIQUE (org_id, scope, avatar_id)   -- one live digest per scope; refreshed in place
```

**What we deliberately do NOT duplicate:** documents (a `document` entity
carries `ref_id = knowledge_documents.id`, never a copy of the text), people
already known to the identity plane (`ref_kind='principal'` →
`knowledge_principals.id`), and actions (`ref_id = ledger_items.action_id`,
the stable cross-channel id minted by `ledger.new_action_id()`).

**Shared org brain vs per-avatar brain.** Both fall out of the same rows:
scope by `org_id` alone = the **shared org brain** (every meeting, every avatar);
add `avatar_id` = the **per-avatar brain**. `memory_digests.scope` selects which
one feeds the brief; the default is `avatar` for the avatar's own recall plus an
`org` digest for company-wide context, both capped (§8).

## 6. Entity resolution (no scatter, no duplicates)

The `UNIQUE (org_id, kind, key)` constraint is the enforcement point — writers
upsert, they cannot create a second node for the same key.

| Kind | `key` | Notes |
|---|---|---|
| person | `email` lowercased, when known | **Grounded caveat:** `session_participants` (`store.py:596`) stores *display names only* — Recall participant events carry no email. Emails come from the calendar path (`main._extract_invite_emails`, `main._calendar_event_organizer_email`), the org's `users.email` rows, and `knowledge_principals.email`. |
| person (fallback) | `dn:` + normalized display name | `resolution='display_name'`, `confidence 0.6`. Never merged automatically with an email node except by the rule below. |
| meeting | `bot_id` | Series identity is the separate `meeting_key` column, so "the same recurring meeting" is a query, not a node. |
| document | `knowledge_documents.id` | Node is a pointer; text stays in the knowledge plane. |
| project | normalized name (casefold, collapse whitespace, strip punctuation) | **Exact-normalized match only creates nodes.** Fuzzy similarity may only create a *link* (`rel='mentions'`, `confidence < 1`), never a new node. |
| decision | `sha1(_norm(text))` per org | Reuses `ledger._norm` — the dedupe function already in production. |
| action | `action_id` | Already stable across live capture → webhook → artifact → ledger. |

**Merge rule (one direction, never automatic across emails).** When a
display-name node later co-occurs with a confirmed email for the same person
(calendar invite for the same `meeting_key`, or a directory match), the
display-name node gets `alias_of = <email node id>` and its edges are rewritten
to the email node in one transaction. Two nodes with *different* emails are
never merged. Merges are recorded in `knowledge_audit` (event
`memory_entity_merged`). No LLM is involved in identity.

## 7. ACL / privacy — ✦ ratified 2026-07-30

Meeting memory is more sensitive than documents: a document has permissions the
customer already set; a meeting has permissions **we invent**. M2's rule is
default-deny; the question is what "allowed" means for a meeting.

**✦ Ratified rule: all-attendees + org-public opt-in + admin-grant.**

A past meeting *M* is retrievable inside meeting *N* iff **every** resolved
human participant of *N* was also an attendee of *M* — or *M* is tagged
`org-public` — or the non-attendee(s) in the room have been **explicitly
granted access to *M* by an org admin** (per-meeting, named principals, written
as additional `knowledge_document_acl` rows and audited). Consequences, on
purpose:

- The recurring team (our ICP) gets full memory: same people, every time.
- A guest joins ⇒ memory narrows to `org-public` (plus anything the admin
  explicitly granted them) automatically, live, with no admin action.
- An **unresolved** participant (display name we could not map) counts as a
  stranger and narrows the set — default-deny extends to identity ambiguity,
  exactly like M2's "no mapping ⇒ empty principal set".
- Admin grants are additive, per-meeting, and auditable — never a blanket
  "this person sees everything".
- Nothing crosses `org_id`, ever (RLS + explicit predicate, as in `0011`).

**Rejected alternative — union of attendees' visibility:** person A (in a
confidential meeting last week) and person B (not) meet today; the union would
let the avatar say the confidential thing out loud to B. Unacceptable —
the leak is *spoken*, which is unrecallable.

**Rejected alternative — org-wide by default:** simplest and probably what many
customers want, but it makes the first enterprise security review harder than
it needs to be, and the opt-in tier + admin grants reach the same outcome with
consent.

Implementation without a new authorization engine: each meeting document gets
`knowledge_document_acl` rows for the `knowledge_principals` of its resolved
attendees (plus the tenant/`everyone` principal when tagged `org-public`, plus
admin-granted principals), and the tool resolves the *current room* into a
principal set and passes it to the existing `retrieval.query()` audience. Two
grounded details this must handle:

1. `_CANDIDATE_FILTER` (`datastore.py:560`) denies documents whose
   `acl_synced_at` is older than `knowledge_acl_stale_seconds` (default 24 h).
   A meeting memory has **no external permission source to re-sync**, so it
   would silently vanish after a day. Fix: the staleness predicate gets an
   explicit exemption for locally-authored sources
   (`OR s.kind = 'meeting'`), with a comment and a regression test — *not* a
   cron job that re-stamps a timestamp to defeat its own check.
2. `settings.knowledge_meeting_audience` is **deployment-wide** (documented in
   `core/config.py:314-321`). Meeting memory must not inherit that blunt switch:
   it gets a per-org setting `meeting_memory_visibility ∈ {attendees, org}`
   (default `attendees`) on the control-plane org row. This is also the
   first per-org knowledge setting — it retires a known M2 wart.

**✦ Retention (ratified): 12 months** of retrievable meeting memory, then cold
tier (§9). Plus a "forget this meeting" admin action (tombstone + chunk delete
+ edge delete) in slice 3.

## 8. Flows

### 8.1 Pre-meeting: the one LLM call

Seam: `backend/app/meeting/lifecycle.py:566` — the existing
`asyncio.gather` of best-effort session-start reads gains a **7th** entry,
`_quiet(run_in_threadpool(meeting_memory.week_brief, org_id, avatar.id))`,
and its result is prepended to `session.memory_brief` exactly like the Drive /
Asana / calendar blocks:

```
[Last 7 days — what the company discussed and decided]
<= 1200 chars, dated lines
```

`week_brief()` (new module `backend/app/memory/meeting_memory.py`):

1. Read `memory_digests` for `(org_id, scope, avatar_id)`. Fresh within
   `MEETING_MEMORY_DIGEST_TTL_SECONDS` (default 1800) ⇒ **return the row, no
   model call**. This is what makes "one LLM call" affordable: it is one call
   per org per half hour, not one per join.
2. Miss ⇒ select the last 7 days from `memory_meetings`
   (`WHERE org_id=? AND ended_at > now()-7d ORDER BY ended_at DESC LIMIT 25`,
   distilled fields only, hard-capped at ~6 000 input chars) and make **one**
   `llm.complete` call with `settings.brain_model_fast` — the same pattern and
   the same model tier as `engine.rolling_summary`
   (`backend/app/brain/engine.py:867`), wrapped in a hard timeout
   (`MEETING_MEMORY_BRIEF_TIMEOUT_SECONDS`, default 6).
3. Timeout, error, or **stub brain** ⇒ deterministic fallback: a bulleted list
   built from the same rows with no model at all. The key-free demo therefore
   still shows real accumulated memory (plainer prose), and a slow model can
   never delay a bot join — the gather is already `_quiet`/best-effort.
4. Write the digest back (upsert on the unique key) and return.

**Restart wrinkle (fixed in the same PR):** `main.py:3089` re-derives
`session.memory_brief` from `carryover_brief` *alone* after a process
replacement, which would silently drop the week block mid-meeting. That
lazy-reload must re-compose the same blocks (digest read is a cache hit, so it
stays cheap).

**Latency budget.** Join path only, never the transcript→token path. Cache hit
≤ 20 ms (one indexed row). Cache miss ≤ 6 s hard cap, concurrent with the five
existing reads, and cancellable. Every-turn cost is bytes only: the digest is
capped at 1 200 chars because `memory` is rendered into **every** live prompt
(`engine.py:711-715`) and input tokens are first-token latency.

### 8.2 In-meeting: rolling linkage (SLICE 2)

Seam: `backend/app/brain/engine.py:859-886`. `rolling_summary()` gains one
optional `context: str = ""` parameter (the week digest), and
`ROLLING_SUMMARY_SYSTEM` gains one sentence: *"When a new line relates to
something in the past-week context, say so in one clause (what, when)."* The
call site is the existing background task
(`main._refresh_rolling_summary`, `main.py:1267`) — off the hot path, unchanged
cadence, **still capped at 120 words** so the running-summary block does not
grow the live prompt.

### 8.3 In-meeting: deep recall, older than a week (SLICE 2)

**New tool `meeting_memory_search`**, not an extension of
`company_brain_search`. Rationale: different corpus, different authorization
rule (attendee-derived, not document ACL), and clearer model tool-selection —
and `company_brain_search`'s contract is covered by the M2 test matrix that we
should not disturb. It reuses everything else: registered in
`backend/app/brain/tools.py` `_DISPATCH` + `_SESSION_TOOLS`, spec appended in
`specs_for()` under the same flag check, un-prefixed (structurally unreachable
from the Cedric MCP action path), read-only, results wrapped by
`retrieval.format_for_model` (untrusted-data framing, delimiter neutralization).

Body (`memory/meeting_memory.py::search`), in order:
1. Re-check the gate **at dispatch time** (M2 rule D6).
2. Resolve the current room → principal set (§7). Empty ⇒ return the honest
   "not permitted / nothing indexed" string and audit `query_denied`.
3. **One** SQL query: ACL-filtered candidates over `kind='meeting'` chunks
   (the existing `retrieval_candidates` path), fused with a structured lookup
   over `memory_meetings`/`memory_edges` for the "when/who" fields.
4. Return ≤ 5 results, each with date, meeting title, attendees and the
   decision/action lines. **No LLM inside the tool.** Hard timeout
   (`MEETING_MEMORY_SEARCH_TIMEOUT_SECONDS`, default 1.5); on timeout return a
   short honest failure string rather than blocking the turn.

Target: p95 < 150 ms for step 3 at 10⁴ chunks/tenant (the M2 scale envelope;
beyond that the swap point is pgvector, isolated inside `retrieval.py`).

### 8.4 Post-meeting: the meeting flows INTO the brain

Seam: `backend/app/meeting/lifecycle.py:978-990`, immediately after
`store.save_artifact` and beside the existing best-effort
`ledger.record_meeting`:

```python
try:
    await run_in_threadpool(meeting_memory.deposit, session, artifact)
except Exception:
    pass   # memory must never block the meter-stop cleanup below
```

`deposit()` does the **cheap, synchronous** part only — write
`memory_meetings` + `memory_attendees` from the artifact and
`session.participants` — then (SLICE 2) **enqueues** a `meeting_ingest` job via
`dal.enqueue_job` for the expensive part (build the distilled document, chunk
with `ingest.chunk_text`, embed with the `sync._embed_chunks` helper, write ACL
rows with `datastore.replace_document_acl`, stamp `acl_synced_at`, upsert
entities/edges). Rationale: `/sessions/{id}/end` is the customer-visible
response *and* precedes session cleanup; it must never wait on embeddings.
Job kind `meeting_ingest` is added by widening the `0011` CHECK
(DROP/re-ADD, as `0011` did to `0010`); the worker loop
(`ingest.process_due`, dispatched from the `main.py` lifespan) gains one branch.

**What is written — distilled only:**
`summary`, `decisions[]`, `actions[]` (+`action_id`, owner, deadline),
`missing_steps[]`, `readiness_score`, `meeting_type`, attendee **display
names/emails**, timestamps. **Never** `artifact["transcript"]`, never an
utterance. A test asserts the transcript string does not appear in any
`memory_*` or `knowledge_*` row for a seeded meeting, and the guard hook already
blocks logging it.

**Entity extraction without a new model call:** projects/topics come from an
optional extra field on the **existing** post-meeting summarizer
(`post_meeting`, called at `lifecycle.py:801`, already Sonnet-class). People,
meetings, documents, decisions and actions are extracted **deterministically**
from structured artifact + participant data — no model in the identity path.

**Safety property worth stating:** the M2 red-team fix restricted
`chunks_for_avatar` / `keyword_search` to `kind IN ('upload','drive')` and made
`dal.assign` refuse connector sources (`dal.py:506,537`). A new source
`kind='meeting'` is therefore **excluded from the un-ACL'd avatar index by
construction** — meeting memory can only ever be reached through the
ACL-filtered `retrieval.query` path.

## 9. Bounded growth — retention and compaction tiers

| Tier | Window | Kept | Feeds |
|---|---|---|---|
| **Hot** | 0–7 days | `memory_meetings` + document + chunks + embeddings + all edges | the week digest, deep recall |
| **Warm** | 7 days–12 months ✦ | same rows; chunks kept, embeddings kept | deep recall |
| **Cold** | > 12 months, or over cap | `memory_meetings` row + entities + edges only; chunks and embeddings deleted, document tombstoned | structured recall ("we met about X on 3 Mar with A and B; decision: …") — no full text |

Caps and compaction, enforced by a `meeting_compact` job on the existing
claim/lease queue (one run per org per day, off-peak — the worker still shares
the API process, risk #6 in `RISKS-AND-MILESTONES.md`):

- **Per series:** when one `meeting_key` exceeds **200** meetings (mirroring
  `ledger._LIST_LIMIT = 200`, oldest-first eviction), the oldest beyond the last
  20 are folded into **one series-digest document** (one LLM call, in the
  worker) and their chunks are deleted.
- **Per org:** `MEETING_MEMORY_MAX_DOCS` (default 5 000 retrievable meeting
  documents) — over cap, the oldest go cold.
- **Edges:** `mentions` edges with `confidence < 0.7` and no reinforcement in
  180 days are dropped (they are the scatter-prone class).

Growth math for a 200-person org at 40 meetings/week: distilled text ~3 KB ×
2 000/yr ≈ 6 MB/yr; embeddings dominate at ~8 chunks × 512-dim JSON ≈ 20 KB per
meeting ≈ 40 MB/yr — which is why the cold tier drops embeddings first.
Chunk deletion is already a permitted runtime operation (`dal.delete_source`);
documents/versions stay tombstone-only, per the `0010`/`0011` grant philosophy.

## 10. Slicing

### NOW — Slice 1: "Laura knows the last week" (smallest thing that proves value)

Migration `0012` (`memory_meetings`, `memory_attendees`, `memory_entities` for
people only, `memory_digests`) · `deposit()` at `lifecycle.py:984` writing
distilled rows + attendees · `week_brief()` with TTL cache, one LLM call, hard
timeout, deterministic stub fallback · injection at `lifecycle.py:566` **and**
the `main.py:3089` restart path · flag `MEETING_MEMORY_ENABLED=false`.
No graph edges, no new tool, no ACL surface yet (slice 1 is org-scoped and
reaches only the org's own avatar in its own meetings — the same trust boundary
as `carryover_brief` today).

**Success criteria**
- In a second meeting the same week on a **different link**, the avatar
  correctly references a decision/action from the first when asked — verified
  in one scripted live pair, plus a key-free test with two seeded meetings.
- Join latency delta ≤ +50 ms p50 (cache hit); p95 bounded by the 6 s cap and
  never fatal (test: model raises ⇒ join still succeeds, brief still non-empty
  via the deterministic path).
- Digest ≤ 1 200 chars in 100 % of generations (hard truncation test).
- Zero transcript text in any new row (assertion test), suite stays green,
  demo runs key-free.
- Flag off ⇒ byte-identical behaviour (inertness test, M2 style).

### NEXT — Slice 2: "Deep recall + the actual graph"

Meeting-as-document into the knowledge plane (`kind='meeting'` source, chunks,
embeddings, ACL rows, staleness exemption) · `memory_entities`/`memory_edges`
populated (deterministic + the summarizer's optional entity field) ·
`meeting_memory_search` tool · rolling-summary `context=` linkage ·
per-org `meeting_memory_visibility` · admin-grant surface (per-meeting named
principals). ✦ Starts only after the owner green-lights it post-Slice-1 audit.

**Success criteria**
- "When did we decide X / who owns Y / what did we agree with <person>" is
  answered mid-meeting from a meeting **older than 7 days**, with date +
  attendees, in ≤ 1 extra model round-trip.
- Tool retrieval p95 < 150 ms at 10⁴ chunks/tenant.
- **Authorization tests in the M2 scenario style:** a meeting whose attendee set
  does not include everyone in the current room is provably not retrievable;
  an unresolved participant narrows to `org-public` + admin grants; cross-org
  retrieval returns nothing under real RLS (embedded Postgres, as
  `test_company_brain_graph_pg`).
- Entity duplication: seeding the same person under two display-name spellings
  plus one email yields **one** surviving node after merge.

### LATER — Slice 3: "It stays clean and it stays small"

Compaction job + tiers + caps · alias merge hardening · "forget this meeting" ·
dashboard "what the brain remembers" (entities, recent meetings, sources) ·
retention runbook → scheduled job (risk #8 in `RISKS-AND-MILESTONES.md`) ·
audit reader for memory queries (risk #9).

**Success criteria**: a simulated 12-month org (24 000 meetings) stays under the
per-org cap with retrieval p95 unchanged; "forget" removes a meeting from every
retrieval path within one job cycle, proven by test.

## 11. Risks and open questions

| # | Risk / question | Recommendation |
|---|---|---|
| 1 | **ACL semantics** — all-attendees vs union vs org-wide. | ✦ RATIFIED 2026-07-30: all-attendees + `org-public` opt-in + admin-grant (§7). |
| 2 | **Identity is the weak link** — `session_participants` has no email; display names are ambiguous and user-controlled. | Email-first from the calendar path/`users`/`knowledge_principals`; display-name nodes marked `confidence 0.6`; never auto-merge distinct emails; unresolved ⇒ narrows visibility. |
| 3 | **PII surface grows** — derived personal data (who met whom, about what) becomes queryable. | No transcripts, ever; the new rows live in the same RLS tier as artifacts (no new exposure class); 12-month retention + "forget this meeting"; document it in `THREAT-MODEL.md` before slice 2 ships. |
| 4 | **Latency on the join path.** | TTL cache + hard timeout + deterministic fallback + best-effort gather (§8.1). Nothing added between transcript and first token. |
| 5 | **Prompt bloat** — memory + summary blocks are in *every* live turn. | Hard caps (1 200 chars digest / 120 words summary) with truncation tests; measure first-token latency before/after in the slice-1 live pair. |
| 6 | **Unbounded growth.** | Tiers + caps + compaction job (§9); embeddings dropped first. |
| 7 | **Hallucinated recall spoken aloud.** | The digest is model-written text injected as fact — label the block as *distilled from past meetings, with dates*, instruct "say you're recalling and offer to check", and keep tool results inside `format_for_model` untrusted framing. |
| 8 | **Model cost.** | One digest call per (org, avatar) per 30 min on the fast model + one optional compaction call/day — negligible against the ~$0.40–0.80 vendor cost of a 30-min meeting (`.claude/CONTEXT.md`). |
| 9 | **Worker shares the API process** (M2 risk #6). | Small page budgets; compaction off-peak; the process split stays a Tier-1 deploy change, not a blocker. |
| 10 | **Repo reality** — this fork is archived; prod is upstream `Dunic15/Laura` (M2 risk #10). | Land on `restructure/repo-structure` and port with the same motion as the restructure branch. |
| 11 | **Open: does the org digest belong in the brief at all, or only the avatar digest?** | Ship avatar-scope first (slice 1), add org-scope behind the same flag once we see whether it earns its 1 200 chars. |
| 12 | **Open: series compaction quality** — folding 180 old standups into one digest may erase the one line someone needed. | Keep the structured `memory_meetings` rows forever (cold tier); only the *text* is compacted. |

## 12. Explicitly out of scope

Transcript storage or search; a graph database; real-time graph updates during
the meeting (ingestion is at finalize); cross-org anything; any change to the
avatar layer, the `{type:"speak"}` contract, `recall_client`/`anam_client`
signatures, or the zero-key demo guarantee.
