# Multi-tenancy, enterprise-ready data layer, day one

**Status: PLAN / DECISION DOC, no code in this doc.** Written 2026-07-09.
Grounded against the real code (`store.py`, `ledger.py`, `avatars/`,
`org_api.py`, `config.py`) and reconciled with the two decisions already on
record: storage durability ([`STORAGE-DURABILITY.md`](STORAGE-DURABILITY.md),
Litestream→S3) and the live integration contract
([`../integration/SURFACE-API.md`](../integration/SURFACE-API.md)).

Companion doc for the sister ask (each avatar knowing the other's skills):
[`../integration/AGENT-CARD.md`](../integration/AGENT-CARD.md).

---

## 0. The one thing to get right

> **Every persisted row carries a non-null `org_id`, resolved server-side from
> the authenticated principal and stamped at `POST /sessions/start`: never
> derived from a request parameter or a meeting participant's name.**

There is **zero** `org_id`/`tenant_id`/`workspace_id` in the codebase today
(grep-verified in `store.py`, `ledger.py`, `config.py`, `.env.example`).
Everything is keyed by `bot_id` (a Recall bot) or `meeting_key` (a meeting-URL
code); one flat tenant. Adding a `NOT NULL org_id` column to a live,
multi-customer table later is exactly the painful, risky migration we want to
avoid. **The store is empty and ephemeral right now, so the retrofit cost is at
its absolute minimum.** This is the "start right so we never re-architect" move.

Everything else in this doc (durable DB, identity tables, RLS, per-org RAG,
SSO, governance) either rides on that key or can be added incrementally without
a retrofit.

---

## 1. The reframe: two data planes, opposite constraints

"Enterprise-ready" is **not** "move everything to Postgres". The live-meeting
path and the control-plane data have opposite constraints, and the storage
decision already on record (Litestream→S3) stays correct for the hot path.

| | **Hot path** | **Control plane** |
|---|---|---|
| Data | `sessions`, `utterances` (written per transcript line, on the event loop); ws routing, `pending_messages`, MeetingState (RAM, process-local) | orgs, users, roles; `ledger_items`, `artifacts`, knowledge, audit, usage, capability cards |
| Constraint | **Latency is the product**: a network round-trip per utterance kills it | none of it is on the live path |
| Store | **stays SQLite local + Litestream→S3** (already decided) | **Supabase Postgres + Row-Level Security**, eu-central-1 |

The bridge between the two planes is the single `org_id`, stamped once at
booking and inherited by everything downstream.

> **Why Supabase** (owner decision, 2026-07-09): the Supabase MCP connector is
> already in hand, RLS + Auth are built in, residency is EU, and it can mint a
> JWT carrying an `org_id` claim immediately; the fastest path to safe
> multi-tenancy. RDS/Aurora keeps everything in one AWS account but makes us
> build RLS/Auth by hand and add a VPC connector.

### Belt-and-suspenders isolation (both planes)

Isolation is enforced **twice**, on purpose:

1. **Explicit `WHERE org_id = ?`** in the DAO (`store.py` / `ledger.py`); this
   also survives the local SQLite demo path, which has no RLS.
2. **Postgres RLS** (`ENABLE` + `FORCE ROW LEVEL SECURITY`, policy
   `org_id = current_setting('app.current_org')::uuid`) as a backstop so a
   forgotten `WHERE` can't leak.

**Streaming caveat (important):** do **not** hold one Postgres transaction open
across a multi-second SSE/answer stream to keep a session-level `SET`: that
pins/exhausts the Supabase pooler and risks the documented GUC-leak footgun
(a reused pooled connection serving the previous request's tenant). Set the org
**per short statement/transaction** (`set_config('app.current_org', :org, true)`
- transaction-local) and keep the live speak/answer path off any long-held txn.

---

## 2. Sequencing

### NOW: the irreversible minimum (do while the DB is empty)

Trimmed to only what is genuinely painful to retrofit. Identity tables, RLS
polish, and the write-behind queue are fast-follow, not blockers.

1. **Durable Postgres control plane, with separate runtime/DDL roles.**
   `LAURA_DATABASE_URL` is the policy-bound `laura_app` pooler credential
   used by App Runner; `LAURA_DATABASE_ADMIN_URL` is the owner credential used
   only by an explicit Alembic migration job. The admin URL is never injected
   into the service. Empty runtime URL keeps the key-free SQLite demo.
   **Adopt Alembic on day one**: NEXT/LATER explicitly add columns (usage,
   audit, pgvector, connectors), so migrations must be repeatable and
   reviewable. The live sessions/utterances hot path remains local SQLite.
   *Files:* `control_plane.py`, `config.py`, `backend/alembic/`; deployment
   mapping: [`PRODUCTION-DATABASE-ROLES.md`](PRODUCTION-DATABASE-ROLES.md).

2. **Define the single meeting→org binding, then `org_id NOT NULL` on every
   table** in the same migration: `sessions`, `utterances`,
   `conversation_routes`, `artifacts`, `scheduled_events`, `ledger_items`.
   Org is resolved **once** at `POST /sessions/start` from the authenticated
   booker's token→org and stamped on `sessions` + `conversation_routes`
   (the only two places identity still exists. Recall webhooks carry only
   `bot_id`, the avatar ws only `conversation_id`). All ledger/artifact/
   utterance writes inherit org **from the session**. Composite keys/indexes
   lead with `org_id`. Backfill existing synthetic data to a fixed
   `DEMO_ORG_ID` in the same migration.
   *Files:* `store.py` (schema), `ledger.py` (schema + index), `main.py`
   (`start_session`).

3. **Scope `meeting_key` by `(org_id, meeting_key)`.** `ledger.meeting_key()`
   derives identity from the Meet/Zoom/Teams code, so two different customers on
   the same recurring-link code would **merge** cross-meeting memory; and
   `carryover_brief` is injected into the **live** prompt, so another org's open
   actions could surface mid-meeting. Pass `org_id` as a required arg through
   the ~12 choke-point functions in `ledger.py`/`store.py`/`org_api.py`.

4. **Two-org leak test in CI, on real Postgres** (Testcontainers or a compose
   service). Assert org B cannot read org A's data through *every* enumeration
   route (`/meetings/list`, `/org/actions`, `/org/search`, sessions). The demo
   staying on SQLite makes RLS a no-op locally, so **this is the only place the
   safety thesis is actually exercised.** Cheapest insurance against the one bug
   class that ends the company. Verify the app connects as a **non-superuser**
   role (superusers bypass RLS) and that `FORCE` is on.

### NEXT: when customer #1–#3 arrives (rides the org_id spine)

- **Identity skeleton** (empty tables now cost nothing): `orgs`, `users`,
  `memberships` (roles here: owner/admin/member/billing), `org_domains`.
  Reserve `sso_connection_id`/`auth_method` so SSO/SCIM slot in with no
  migration. Meeting participants (Recall gives only display-name/email)
  resolve against `org_domains`: **verified-domain → member; unknown/consumer
  domain → guest** (transcript stays memory-only PII, no ledger writes). See
  the security note in §3.
- **Per-org RAG + per-org index.** Today `.index.json` is keyed by `avatar_id`
  only over one global committed corpus; one company's docs can surface in
  another's answers. Demo org keeps the local folder/`.index.json`; real orgs
  get a `knowledge_chunks(org_id, avatar_id, visibility, allowed_principals[],
  embedding)` pgvector table with an RLS `org_id` filter. This is the real moat.
- **Per-org metering, governance, per-org connectors/credentials.** The
  per-minute meter already fires at session end → with `org_id` it becomes a
  per-tenant invoice line + a quota gate for free. Add append-only `audit_log`
  (metadata only, never transcript), configurable retention, GDPR
  delete/export. Move the single global Google/SendGrid/Slack identities to
  per-org config (today one global account = cross-tenant leak + impersonation).

### LATER: demand-driven

- **WorkOS** for SAML/OIDC SSO + SCIM, federating onto the orgs/users/
  memberships already built (first enterprise gate; free until first
  connection).
- **Document-level ACLs / permission-aware retrieval**: populate
  `allowed_principals` from real connector ACLs so an HR/comp doc never surfaces
  to a non-HR employee (isolates person-from-person *inside* an org). The field
  is reserved in NEXT, so it's populating a column, not reshaping the index.
- **Physical isolation for a whale/regulated tenant** (schema- or DB-per-tenant
  behind the same control plane); only for a contractual isolation/residency
  demand.
- **Trust artifacts**: sub-processor page, Art.28 DPA, security page (near-free,
  ship early); SOC 2 Type II when enterprise demand is real.

---

## 3. Gotchas the adversarial review caught (decide before writing org_id)

1. **Org must NOT be derived from the participant: spoof hole.** Granting
   "member, may write to the ledger" on a verified-domain match against the
   Recall display-name/email is a security-model error: that attribute is
   unauthenticated (anyone joins Zoom as `cfo@bigco.com`). Ledger/artifact org
   **derives from the session**, bound at booking by an authenticated booker.
2. **Who is the booker?** Today Cedric books with **one shared service
   credential**, and calendar auto-join is spawned by a webhook with **no human
   principal at all**. If Cedric stays a single principal, every session stamps
   *Cedric's* org, not the customer's. **This is an open question for Ben** (see
   [`AGENT-CARD.md`](../integration/AGENT-CARD.md) §open-questions): either
   Cedric becomes a per-org principal (token→org) or he carries the customer's
   org explicitly in the booking. Resolve before populating `org_id`.
3. **Don't keep Postgres only in prod.** SQLite demo → RLS is a no-op locally
   and there's no two-tenant test. RLS you never exercise is decorative. The
   NOW leak test + a Postgres CI service fix this.
4. **Per-utterance writes stay off Postgres on the hot path.** Moving
   `add_utterance`/`mark_spoke` to a network DB adds round-trips to the live
   speak path. Keep the hot path on local SQLite (Litestream); if org-memory
   writes ever move to Postgres, use a write-behind queue.
5. **Postgres fixes durability, not horizontal scale.** `_sessions`, ws routing,
   `pending_messages`, and rag `_CACHE` are process-local; the app is pinned
   `MaxSize=1`. Nobody should read "enterprise-ready data layer" as "we can run
   N instances": that's a separate ws/session-routing redesign.
6. **`orgs.region` is aspirational** until there is a second Supabase
   project/region. Don't surface a residency promise the single eu-central-1
   project can't honor.

---

## 4. Concrete schema sketch (migration 0001)

```sql
-- Target: Supabase Postgres, eu-central-1. App connects as a dedicated
-- NON-superuser role `laura_app` (superusers bypass RLS). Local key-free demo
-- keeps the SQLite file; this is the Postgres shape the DAO maps onto.

-- identity spine (empty now; SSO/SCIM attach later)
CREATE TABLE orgs (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text NOT NULL,
  slug           text UNIQUE NOT NULL,
  plan           text NOT NULL DEFAULT 'free',
  region         text NOT NULL DEFAULT 'eu-central-1',   -- aspirational (see §3.6)
  sso_connection_id text,                                -- reserved for WorkOS
  retention_days int  NOT NULL DEFAULT 90,
  created_at     timestamptz NOT NULL DEFAULT now(),
  deleted_at     timestamptz                             -- right-to-erasure
);
INSERT INTO orgs (id, name, slug, plan)
VALUES ('00000000-0000-0000-0000-0000000000de','Demo','demo','demo');  -- the demo is just an org

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext UNIQUE NOT NULL, name text,
  external_idp_subject text, provider text,
  auth_method text NOT NULL DEFAULT 'token',             -- token|password|oauth|saml
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE memberships (                               -- many-to-many; roles live HERE
  user_id uuid NOT NULL REFERENCES users(id),
  org_id  uuid NOT NULL REFERENCES orgs(id),
  role text NOT NULL DEFAULT 'member',                   -- owner|admin|member|billing
  status text NOT NULL DEFAULT 'active',
  PRIMARY KEY (user_id, org_id)
);
CREATE TABLE org_domains (                               -- verified-domain member/guest split
  org_id uuid NOT NULL REFERENCES orgs(id),
  domain text NOT NULL,                                  -- NEVER map gmail.com etc.
  verified_at timestamptz,
  PRIMARY KEY (org_id, domain)
);

-- existing store.py tables, now org-scoped (org_id leads every key/index)
CREATE TABLE sessions (
  org_id uuid NOT NULL REFERENCES orgs(id),
  bot_id text NOT NULL,
  meeting_url text NOT NULL, avatar_id text NOT NULL DEFAULT 'laura',
  anam_conversation_id text NOT NULL DEFAULT '',
  anam_conversation_url text NOT NULL DEFAULT '',
  last_spoke_at double precision NOT NULL DEFAULT 0,
  proactive_done boolean NOT NULL DEFAULT false,
  integration jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, bot_id)
);
CREATE TABLE conversation_routes (
  org_id uuid NOT NULL REFERENCES orgs(id),
  conversation_id text NOT NULL, bot_id text NOT NULL,
  PRIMARY KEY (org_id, conversation_id)
);
-- the ws/{conversation_id} entry point is unauthenticated and knows ONLY the
-- conversation_id → it must resolve to its org BEFORE any read:
CREATE UNIQUE INDEX uq_conv_global ON conversation_routes(conversation_id);

CREATE TABLE utterances (            -- PII: DB-only, never logged, never wired out
  org_id uuid NOT NULL REFERENCES orgs(id),
  bot_id text NOT NULL, speaker text NOT NULL, text text NOT NULL,
  ts double precision NOT NULL,
  FOREIGN KEY (org_id, bot_id) REFERENCES sessions(org_id, bot_id)
);
CREATE INDEX idx_utt_org_bot ON utterances(org_id, bot_id);

CREATE TABLE artifacts (             -- distilled only (wire_artifact strips transcript)
  org_id uuid NOT NULL REFERENCES orgs(id),
  bot_id text NOT NULL, artifact jsonb NOT NULL,
  visibility text NOT NULL DEFAULT 'participants',       -- participants|org|private
  saved_at timestamptz NOT NULL DEFAULT now(),
  delete_by timestamptz,                                 -- stamped from orgs.retention_days
  PRIMARY KEY (org_id, bot_id)
);

-- ledger.py, now org-scoped (fixes the cross-org meeting_key merge)
CREATE TABLE ledger_items (
  org_id uuid NOT NULL REFERENCES orgs(id),
  id bigint GENERATED ALWAYS AS IDENTITY,
  meeting_key text NOT NULL,                             -- scoped by org_id now
  avatar_id text NOT NULL, kind text NOT NULL,
  item text NOT NULL,                                    -- distilled, never raw transcript
  item_norm text NOT NULL,
  owner text NOT NULL DEFAULT '', deadline text NOT NULL DEFAULT '',
  meeting_type text NOT NULL DEFAULT '', status text NOT NULL DEFAULT 'open',
  bot_id text NOT NULL DEFAULT '', created_at double precision NOT NULL,
  resolved_at double precision, resolved_by_bot_id text NOT NULL DEFAULT '',
  PRIMARY KEY (org_id, id),
  UNIQUE (org_id, meeting_key, kind, item_norm)          -- dedupe now org-scoped
);
CREATE INDEX idx_ledger_org_key_status ON ledger_items(org_id, meeting_key, status);

-- governance + metering (org spine makes these near-free; add in NEXT)
CREATE TABLE audit_log (             -- append-only, METADATA ONLY, no transcript
  org_id uuid NOT NULL REFERENCES orgs(id),
  actor_user_id uuid, action text NOT NULL, target text,
  ts timestamptz NOT NULL DEFAULT now()
);
REVOKE UPDATE, DELETE ON audit_log FROM laura_app;       -- append-only in practice

-- RLS: isolation becomes a DB invariant. Apply this template to every table above.
ALTER TABLE ledger_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_items FORCE  ROW LEVEL SECURITY;      -- owner can't bypass
CREATE POLICY tenant_isolation ON ledger_items
  USING      (org_id = current_setting('app.current_org', true)::uuid)
  WITH CHECK (org_id = current_setting('app.current_org', true)::uuid);
```

### Request-edge wiring (FastAPI)

- New `deps.py` dependency resolves `org_id` **server-side** from the principal
  (Bearer token→org row, or a Supabase JWT `org_id` claim); the unauthenticated
  demo path pins `DEMO_ORG_ID`.
- Replace `cedric.auth_error`'s single global Bearer with a token→org mapping;
  keep the open path only for `DEMO_ORG_ID`.
- `POST /sessions/start`: write `sessions.org_id` + `conversation_routes.org_id`
  at creation.
- Recall webhook (`bot_id` only) and `ws/{conversation_id}` (conversation_id
  only): recover org via the routing tables, then `set_config` **per short
  txn** for the downstream reads (never a request-long transaction; see §1).
- `ledger.py`/`store.py`/`org_api.py`: `org_id` becomes a required first arg.

---

## 5. Open questions for the owner

1. **Cedric as principal** (blocks the meeting→org binding): per-org token now,
   or shared service credential with the org carried explicitly in the booking?
   *Flagged as "depends on Ben" (2026-07-09).* Tracked in
   [`AGENT-CARD.md`](../integration/AGENT-CARD.md).
2. **Auth for first customers**: mint a Supabase-Auth JWT (`org_id` claim) now
   and add WorkOS only at the first SAML deal? (Recommended.)
3. **Default artifact visibility**: participants-only (safer) vs whole-org
   (convenient)? Global retention default 90 days OK, or per-meeting-type?
4. **Any near-term customer likely to demand physical isolation** (own DB/
   region)? If yes, prioritize the store-routing seam earlier.
