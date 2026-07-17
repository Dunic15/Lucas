# Company Brain, Skills & Browser Operator — M0–M6 Roadmap

**What this is:** the implementation roadmap that takes Laura from "meeting avatar
with approved one-shot actions" to "org-personalised avatar that runs reviewed,
multi-step skills — including in a live browser — on a durable company brain".
Each milestone states its goal, what already exists in the code to build on
(with exact paths), the new tables/modules it adds, its acceptance gate, and its
risks.

**Status:** M0 is SHIPPED (this branch, migration `0009_canonical_actions`).
M1 is IN PROGRESS this session. M2–M6 are specced here, not started.
M5 has a full companion spec:
[`LAURA-SABLE-BROWSER-OPERATOR-IMPLEMENTATION.md`](LAURA-SABLE-BROWSER-OPERATOR-IMPLEMENTATION.md).

---

## Invariants every milestone inherits

These are load-bearing facts of the running system; a milestone that violates
one is wrong by definition.

| Invariant | Where it is enforced today |
|---|---|
| Live-meeting contract: Recall renders the avatar page as the bot camera (`output_media` → `camera` → `kind=webpage`) | `backend/app/recall_client.py:305-313`; page URL built in `backend/app/main.py:2120-2124` |
| No inbound WebSocket in production (App Runner 403s upgrades); delivery to the page is SSE `/avatar/stream/{conversation_id}` + a 2 s HTTP poll, both draining ONE at-most-once queue | `frontend/talk.html:694-712`; queue in `store.queue_avatar_message` |
| New page features ride additive `{type: ...}` control messages — `main._send_avatar_control` (`backend/app/main.py:3991`) + a branch in `handleBackendMessage` (`frontend/talk.html:673-689`); `raise_hand`/`lower_hand` is the working precedent (`main.py:4004-4042`) | shipped |
| Per-meeting infra lifecycle hook: fire-and-forget start after `create_bot` succeeds, end only in the leave-verified finalize branch, never between `leave_call` and the usage-row close | `backend/app/gpu_runtime.py`; call sites `main.py:2276-2277` and `main.py:2997-2998` |
| Durable tables: FORCE RLS + `tenant_isolation` policy on `NULLIF(current_setting('app.current_org', true),'')::uuid` TO `laura_app`; REVOKE-then-GRANT exactly SELECT/INSERT/UPDATE; hand-written raw-SQL Alembic migrations (committed head: `0009_canonical_actions`; `0010_company_brain` in flight with M1) | `backend/alembic/versions/0009_canonical_actions.py:108-140` |
| Engine via `control_plane._get_engine()` (role-verified); workers = asyncio loops started in the `main.py` lifespan + `FOR UPDATE SKIP LOCKED` lease claims | `backend/app/control_plane.py:107`, `backend/app/main.py:120-190`, `backend/app/outbox_pg.py:1210-1290` (`claim_due`) |
| Config: pydantic-settings fields in `backend/app/config.py` (lowercase snake_case → UPPER_SNAKE env); `.env.example` with box-drawing dividers, lowercase booleans, no inline comments on empty values | `backend/app/config.py:13-18`, `.env.example` |
| Key-free demo: zero keys runs the whole demo (`stub` brain + `hash` embedder); every new flag defaults off and no-ops cleanly without its vendor keys | `backend/app/embeddings.py`, `.env.example` header |
| Latency is the product on the live path: nothing new on transcript→speak; heavy work happens at session start/finalize or in workers | `backend/app/security.py` docstring, `drive_client.py` design rules |
| Transcripts are PII: memory only, never logged, never sent to third parties beyond the contracted path; `avatars/*/knowledge` in git is synthetic only | `CLAUDE.md`, guard hooks |

---

## Milestone map

| M | Name | State | One-line outcome |
|---|---|---|---|
| M0 | Canonical Action Control Plane | **SHIPPED** | One durable Action object, every surface, exactly-one execution |
| M1 | Durable Company Brain | **IN PROGRESS** | Org-scoped documents + retrieval in Postgres, not git folders |
| M2 | Org-personalised avatars | specced | Same repo avatar, per-org identity/knowledge/toggles |
| M3 | Skill Definition v1 + runtime | specced | Declarative multi-step skills; every write is a canonical action |
| M4 | Skills Studio | specced | Non-engineers author, dry-run, version and publish skills |
| M5 | Live browser operator | specced (companion doc) | Watchable, approvable browser work inside the meeting |
| M6 | Reviewed learning + evaluation | specced | Skills improve via reviewed diffs and regression evals, never silently |

---

## M0 — Canonical Action Control Plane (SHIPPED)

**Goal.** One canonical Action object per consequential write, converged on by
every surface (Laura dashboard, Cedric in Slack) and every route (native,
Cedric, later browser), with exactly-one execution guaranteed across App Runner
instances. Full contract: `docs/product/UNIFIED-ACTION-CONTROL-PLANE.md`.

**What shipped** (branch `claude/m0-canonical-actions`, commit `3fd77cb`):

- **Durable spine**: `queued_actions` extended by migration `0009` with
  `typed_json`, `params_schema_json`, `risk`, `execution_route`
  (`native | cedric | browser` — the CHECK already admits `browser`),
  `origin_avatar`, `receipt_json`, bounded `logs_json`, an enforced
  `UNIQUE(org_id, idempotency_key)` and `execution_lease_until`
  (`backend/alembic/versions/0009_canonical_actions.py`).
- **Lifecycle**: `'' → needs_details ⇄ proposed → approved → executing →
  done | failed`, plus `rejected`. Vocabulary and deterministic
  `needs_details` derivation live in `backend/app/action_plane.py`
  (`PARAMS_SCHEMAS`, `RISK_BY_TYPE`, `missing_params`) — the fix for the
  2026-07-16 "approved but nothing executed" class of failure.
- **Exactly-one execution**: durable first-write-wins decision record
  (`action_decisions`, `ledger.record_action_decision`) plus an atomic
  execution-claim CAS to `executing`
  (`backend/app/outbox_pg.py:792` `claim_action_execution`), proven under real
  concurrency in `test_canonical_actions_pg.py::test_claim_race_has_exactly_one_winner`.
- **Doors**: `POST /org/actions/{id}/approve` (per-org bearer) and
  `POST /dashboard/actions/{id}/approve` (cookie + same-origin) both record the
  decision before dispatch; the params door
  (`org_api.apply_param_edits`, `backend/app/org_api.py:627`) is the only seam
  that changes a typed spec; `GET /org/actions/{id}` /
  `GET /dashboard/actions/{id}` return the canonical view.
- **Events**: `action.requested` (durable outbox), `action.status`,
  and the new `action.updated` after a params edit; peer statuses normalised at
  the boundary (`executed → done`, `declined → rejected`).

**Acceptance gate (met).** Double approval from two surfaces → one external
write, replay answered from the recorded decision; concurrency test green;
key-free demo byte-identical (SQLite `action_approvals` fallback).

**Carried risks / explicitly deferred** (from the M0 doc): Cedric's Slack Edit
modal; re-dispatch of dependency-blocked approvals. Landed since: Cedric-side
DB-enforced idempotency (`laura_exec_claims`, Cedric PR #41) and the
stale-`executing` reconciler (`backend/app/action_reconcile.py`, hardening
slice — calendar claims read-verified, unverifiable types settle
`execution_unknown` after a grace period; the gate before
`ACTION_DISPATCH_ASYNC` may be enabled). M3's `verify` steps remain the
richer per-skill reconciliation.

---

## M1 — Durable Company Brain (IN PROGRESS)

**Goal.** Give an org's knowledge a durable, tenant-scoped home: documents an
org uploads or connects (Drive first) become retrievable knowledge for that
org's avatars, with provenance and versioning, without ever touching the repo
— and without dying with the App Runner instance disk, which is where per-org
indexes live today.

**What exists to build on (at HEAD).**

- **Per-avatar RAG** — `backend/app/rag.py`: markdown chunked by heading
  (~760 chars, 160 overlap), embedded, stored in `avatars/<id>/.index.json`
  (`INDEX_VERSION 3`); retrieval is cosine top-k with source file + section
  for citations; deliberately dependency-light (numpy + JSON).
- **The retrieval seam already takes an org** — `rag.retrieve(avatar, query,
  k, org_id=...)` (`rag.py:471`) merges an org's PRIVATE index
  (`<org>__<avatar>.index.json`, `org_index_path`, `rag.py:505-509`) with the
  avatar's base pack, with an in-process `_ORG_CACHE`. **The durability gap:**
  those index files live on the instance disk, so org knowledge dies on every
  deploy — the exact gap M1 closes.
- **Pluggable embedders** — `backend/app/embeddings.py`: `hash` (key-free
  default), `local` (fastembed), `voyage`; one `embed()` seam. Boot never
  depends on a model download (the 2026-07-16 HuggingFace-504 lesson is
  encoded in the module).
- **Session-start briefs** — `drive_client.py` (Drive folder → capped 8 KB
  brief, cached, never on the live path) and the `memory_brief` channel prove
  the "fetch once at session start, bounded, best-effort" discipline.
- **Durable artifacts** — migration `0007_durable_artifacts`: org-scoped
  meeting artifacts already live in Postgres with retention
  (`orgs.retention_days`).

**New tables/modules (in progress this session — migration
`0010_company_brain` + the `backend/app/knowledge/` package).**

| Piece | Shape |
|---|---|
| `knowledge_sources` | an org's connected source (`upload \| drive`); FORCE RLS + tenant_isolation per convention |
| `knowledge_documents` / `knowledge_document_versions` | one file per document; immutable extracted text per version, deduped by checksum (fixes the "freshness is embedder/version only" gap with content hashes) |
| `knowledge_chunks` | retrieval units + a generated `tsvector` (the FTS half of hybrid search). Deliberate, documented deviation: `laura_app` gets DELETE here — and only here — because chunks are derived data, always rebuildable from a document version; sources/documents tombstone via status |
| `knowledge_assignments` | which avatars may retrieve a source (the org-side successor to git `knowledge_packs`) |
| `knowledge_sync_jobs` | claim/lease ingestion jobs — the `callback_outbox` worker pattern (`FOR UPDATE SKIP LOCKED`); nothing runs on the live path |
| `backend/app/knowledge/` | `dal.py`, `ingest.py`, `router.py` (the `/org/knowledge` API), `storage.py` (raw files → S3-compatible bucket via IAM role, or a local directory next to the SQLite store so dev stays key-free) |
| `rag.build_org_index_from_chunks` | the bridge: Postgres (`knowledge_chunks`) is the durable truth; per-(org, avatar) index files the live path ranks in memory are REBUILT from the tables at ingest and at boot, embedding with the CURRENT provider so index and query vectors can never disagree |
| Config | `company_brain_enabled: bool = False` master switch (OFF ⇒ no tables touched, no worker, retrieval byte-identical); `knowledge_bucket` / `knowledge_s3_endpoint` / `knowledge_aws_region`; `knowledge_max_file_bytes` / `knowledge_max_extracted_chars` guardrails; `openai_api_key` for `EMBEDDING_PROVIDER=openai` (text-embedding-3-small at fixed 512 dims — the recommended durable embedder) |

**Acceptance gate.** (1) Org A uploads a doc; the next meeting its avatar
answers grounded from it with a citation. (2) A deploy (fresh disk) does not
lose org knowledge: indexes rebuild from Postgres at boot. (3) A two-org RLS
test proves org B can never retrieve org A chunks (0006/0009 test style).
(4) Key-free demo byte-identical — flag off by default, repo avatars keep
answering from their folder index with the `hash` embedder.

**Risks.** Real customer docs are PII the repo constraint never had to handle:
DB/bucket-only, never git, never logs (chunk text must never hit
`print`/`logger` — the guard hook only covers transcripts, so this is a
review-time rule). Prompt bloat is latency — the existing brief caps
(`MAX_BRIEF_BYTES`, `MAX_BRIEF_CHARS`) bound every injected byte. Embedder
drift is handled by rebuilding indexes with the current provider, but a
provider switch triggers a full re-embed — the sync-jobs worker must absorb
that without blocking boot (boot never waits on a model, existing rule). The
DELETE-grant deviation on `knowledge_chunks` must stay exactly that narrow.
Multi-instance staleness (instances do not share a disk, so a rebuild on one
instance left the others serving stale files until reboot) is CLOSED by the
hardening slice: a monotonic epoch (max rebuild-job id, `dal.knowledge_epoch`)
plus per-instance sidecar markers, converged by every instance's worker loop
(`ingest.refresh_local_indexes`, ≤60 s bound) — the gate before
`COMPANY_BRAIN_ENABLED` goes to production.

---

## M2 — Org-personalised avatars (IMPLEMENTED — branch claude/m2-org-avatars)

**Goal.** One repo avatar, many org identities: an org can rename the avatar,
adjust its persona, pick its voice/body, bind its Company Brain collections and
flip its capability toggles — without anyone touching `avatars/<id>/` in git.

**What shipped.** Migration `0011_org_avatars` (`org_avatars`,
`org_avatar_versions` — one editable draft, immutable published history,
optimistic `version_token`, FOR-UPDATE single-winner publish —
`org_avatar_assignments` (`org_default` | `user` scopes),
`org_avatar_audit` INSERT-only by grant). The allowlisted overlay vocabulary
lives in `backend/app/avatar_overlay.py` (typed, bounded, unknown fields
rejected, `enabled_tools` may only narrow the canonical ceiling =
{google, slack} ∪ `native_tools`); `backend/app/avatar_resolver.py` is the ONE
resolver every runtime path uses — dispatch resolves once and stashes on the
session (frozen for the meeting, the mission model), hot-path readers use the
zero-I/O `for_session`, doors re-check `family_allowed` at execution time,
and with `ORG_AVATAR_OVERLAYS_ENABLED=false` (default) `resolve()` returns
the exact `avatars.load` cached instance. Selection precedence: explicit
request > user assignment > org default > `DEFAULT_AVATAR_ID`. The context
scope (`{knowledge_source_ids, include_org_default, labels, purpose}`) rides
the resolved avatar into `rag.retrieve`, masking the org index per source id
BEFORE ranking — an empty restricted scope means "no org sources", never
"all"; this object is the seam the future Company Data Foundation's
ContextResolver replaces. Surfaces: `/org/avatars/*` +
`/dashboard/avatar-studio/*` (strict per-branch method checks; admin gate =
personal-org owner, durable role owner|admin via `member_role(member_uid)`,
or the org machine bearer) and the dashboard **Studio** tab (editor, preview
with warnings, publish/history/rollback, org-default assignment).
Parameterized the previously hardcoded "You are Laura" in
`ANSWER_STREAM_SYSTEM` (`{name}`). Known limitations: calendar-autojoin
sessions keep the canonical page URL (face/body) because org attribution
happens after the URL build; overlay convergence on other instances is the
resolver's 60 s TTL; the pre-existing GLOBAL `avatar_capabilities` /
`avatar_brain_mode` tables (cross-org authority) remain the flag-off legacy
path — org-scoped narrowing now exists via overlays, and migrating those
global toggles is deferred work. Enabling in prod requires the about-doc
update (`avatars/laura/about/`) in the same change, per CLAUDE.md.

**What exists to build on.**

- **Avatar = folder, no code changes** (`backend/app/avatars.py` docstring):
  `avatar.yaml` already externalises name, role, wake words, `persona_prompt`,
  voice ids, `talk_body`, `drive_folder_id`, notetaker mode, `knowledge_packs`.
  The overlay only needs to re-source these fields, not invent them.
- **Per-avatar capability toggles are already enforced at the executor seam**
  — `executor.py` `_CAPABILITY_FAMILY` (google/asana families) and the
  per-avatar Slack toggle enforced on the live path (commit `3a83712`, #265).
- **The page is already parameterised** — the bot-camera URL carries
  `avatar_id`, `conversation_id`, `body`, `face_fallback`
  (`backend/app/main.py:2120-2124`); per-org presentation rides the same query
  params, no page rework.
- **Dashboard** — `dashboard.py` cookie + same-origin surface with working
  edit doors (the M0 params door is the validation precedent).

**New tables/modules.**

| Piece | Shape |
|---|---|
| `org_avatar_profiles` (next migration) | `(org_id, avatar_id, overrides jsonb, updated_at)`; overrides = display name, persona addendum, voice id, wake words, capability toggles, drive folder, brain collection ids. FORCE RLS + grants per convention |
| Overlay resolver | `avatars.load_for_org(avatar_id, org_id)` — repo yaml is the base, the profile overlays it; resolved once per session at dispatch (`_start_session` already loads the avatar there), never per turn |
| Dashboard editor | one page: rename, persona addendum (bounded), voice, toggles, knowledge binding; server-side validation in the `validate_param_edits` style |

**Acceptance gate.** Two orgs dispatch the same repo avatar and get different
name/persona/knowledge in-meeting; toggles are enforced at the executor seam
per org; key-free demo (no control plane) behaves byte-identically; no file
under `avatars/` changes at runtime.

**Risks.** Org-authored persona text enters the live prompt — it must be a
bounded *addendum* that can never override the contract/PII/honesty rules
(system prompt stays repo-owned). Token budget: every overlay byte is live-path
latency, so cap like the tool-registry brief (≤700 chars,
`tool_registry.py`). Laura's self-knowledge rule still applies to the *base*
avatar (`avatars/laura/about/` + re-ingest).

---

## DF0–DF1 — Company Data Foundation (IMPLEMENTED — branch claude/df0-df1-data-foundation)

**Contract.** Accepted Handshake contract v5 (`hsk_con_nqrcynzgg746jrhkq647`)
plus six binding acceptance clarifications — the governing text is recorded in
the session and mirrored in the PR description. Laura is the sole owner of the
Data Foundation and the ContextResolver; Cedric is not in the DF path.

**What shipped.** Migration `0012_data_foundation`: `df_connectors` (status
incl. `needs_reconnect`/`acl_incomplete`; `trusted_email_issuer` gate),
`df_source_records` (stable identity + head state, `acl_mode` DEFAULT
`unknown` = fail-closed) / `df_source_record_versions` (immutable, deferrable
composite circular head FK), `df_connector_cursors` (COMMITTED cursor,
advanced only inside the accepted-batch transaction), `df_sync_runs`
(`parked`/`dead_letter` after the retry ladder), `df_quarantine` (**no runtime
DELETE grant** — open rows structurally undeletable; backpressure parks
ingestion at 1000 open rows) + `df_purge_audit` (two-phase payload cleanup),
`df_identities` / `df_principal_bindings` (audited; automatic binding only
for verified emails from `trusted_email_issuer` connectors) /
`df_identity_edges` / typed `df_acl_entries` (surrogate PK + partial
uniques). Purges happen ONLY through `laura_private.purge_quarantine` /
`purge_record_versions` — SECURITY DEFINER, org verified against the
transaction context, cutoff clamped server-side by `orgs.retention_days`.
`backend/app/datafoundation/`: envelope validation (fail-closed `acl_mode`),
DAL (advisory-locked single-winner upserts; body-checksum dedupe never
suppresses metadata/ACL/deletion changes; tombstone/resurrection lineage),
connectors (`upload` wrapping the M1 publish flow + backfill; **network-free
fake Drive** speaking the real Changes-page-token protocol — watermark
incrementals are forbidden; other kinds explicitly deferred), sync worker,
ContextResolver (six-way intersection incl. connector eligibility; degraded
responses preserve the EXACT M2 mask and never widen; inaccessible-record
counts only behind the admin debug route), `/org/data/*` + strict
`/dashboard/data/*` twin (org/principal from authenticated context only —
client `org_id` is a 403 on mismatch). The live per-org index contains
org_default content BY CONSTRUCTION (`chunks_for_avatar` exclusion, fail
closed).

**Deployment order (binding).** `alembic upgrade head` (0012) →
`DATA_FOUNDATION_ENABLED=true` → upload backfill (`POST
/dashboard/data/backfill`) → per-org Drive opt-in. Rollback = flag off; the
migration is additive-only and `downgrade` raises.

**Deferred from DF.** Real Drive HTTP client (credential-gated; the fake
speaks the identical protocol), slack/notion/crm connectors (kind rows legal,
sync parks `not_implemented`), materialized container tree (Studio feedback
loop), cross-org retention scheduling (per-org endpoint + worker piggyback
today).

---

## M3 — Skill Definition v1 + runtime

**Goal.** A declarative, versioned Skill: trigger + typed inputs + a linear
sequence of read/tool/write steps, where every write step mints a canonical
M0 Action (route `native | cedric | browser`) and blocks on approval. Skills
survive deploys: runs are durable rows, not in-memory tasks.

**What exists to build on.**

- **The action plane is the write substrate** — typed schemas, risk classes and
  the idempotency key are already centralised (`action_plane.py`); the executor
  already turns approved typed actions into vendor calls (`executor.py`); the
  claim CAS makes "a skill retried a write" impossible by construction.
- **Session capability registry** — `tool_registry.py` assembles what THIS
  avatar can do in THIS meeting (native vs Cedric vs not-connected) at session
  start; a skill's step preflight is the same check, reused.
- **Deterministic step engines already exist** — the flag-gated find-a-time
  scheduler (`scheduler.py`, #261/#264) and `autopilot.py` are single-purpose
  precursors; `avatars/laura/process_templates/` shows the declarative-template
  direction.
- **A first stale-`executing` reconciler shipped with the hardening slice**
  (`action_reconcile.py`: calendar read-verify + grace settle). Skills still
  add `verify` steps (read back what the write should have produced) as the
  general, per-skill-defined reconciliation for `execution_unknown` outcomes.

**New tables/modules.**

| Piece | Shape |
|---|---|
| Skill Definition v1 | YAML: `id, version, trigger (intent/schedule/event), inputs (typed, action_plane-style schema), steps[]`; step kinds: `read` (brain/browser/tool query), `write` (typed action → canonical Action), `verify` (read-back assertion), `narrate`. Repo skills in `avatars/<id>/skills/`; org skills durable (M4) |
| `skill_runs` + `skill_run_steps` (next migration) | durable run state: `(org_id, run_id, skill_id, skill_version, status, current_step, context jsonb)`; FORCE RLS + grants per convention; steps carry `action_id` when they minted one |
| `backend/app/skills.py` | loader/validator + step interpreter; **no branching in v1** — linear steps, fail-closed |
| Runtime worker | lifespan asyncio loop; claims runnable runs via `FOR UPDATE SKIP LOCKED` lease; a run waiting on approval parks durably and resumes when the action's decision lands (`ledger.set_action_decision_result` is the named hook) |

**Acceptance gate.** A two-write skill (draft email → approval → send → verify
via read-back) completes end-to-end with exactly-one execution per write and a
durable, replayable run trail; killing the process mid-run and rebooting
resumes from the durable row with no duplicate external write; key-free demo:
skills with no connected tools no-op with a clear "not connected" trail.

**Risks.** Workflow-engine scope creep — v1 is deliberately linear, no
conditionals, no loops. Long-lived runs vs App Runner rollouts — the lease +
resume-on-boot pattern must be tested with a deploy mid-run. Approval latency
is a product problem (runs park for hours): parked runs must be loud in the
dashboard, not silent.

---

## M4 — Skills Studio

**Goal.** Non-engineers author, edit, dry-run and publish org skills from the
dashboard, with versioning: published versions are immutable and runs pin the
version they started on.

**What exists to build on.** The dashboard's cookie + same-origin doors and the
params-door pattern (server-side schema validation, refusal after a decision)
are the exact editing discipline to copy (`org_api.apply_param_edits`,
`action_plane.validate_param_edits`). The Skill Definition validator from M3
is the Studio's server-side gatekeeper — the Studio never gets its own parser.

**New tables/modules.**

| Piece | Shape |
|---|---|
| `org_skills` + `org_skill_versions` | draft rows + immutable published versions; publish stamps the validator's verdict; FORCE RLS + grants per convention |
| Studio pages | v1 UI is a schema-validated YAML editor with inline errors + a step-list preview — not a visual builder |
| Dry-run mode | executes `read` steps for real, **simulates** `write` steps by rendering the canonical Actions they would mint (type, params, risk, route, approvers) without inserting them |
| Publish gate | preflight against the org's tool registry: a skill referencing an unconnected tool cannot publish |

**Acceptance gate.** A non-engineer edits a skill in the browser, dry-runs it,
sees the exact approval cards it would create, publishes; the next run uses the
new version while an in-flight run visibly stays on its pinned version.

**Risks.** UI scope creep (hold the YAML-editor line for v1). Privilege: the
Studio must never let a skill exceed the org's connected capabilities or lower
an action's risk class — risk stays derived from `action_plane`, never
author-set. Org-authored narration strings enter meetings: bound and sanitise
like the M2 persona addendum.

---

## M5 — Live browser operator

**Goal.** Laura performs browser work during the meeting — visibly (live view
on her camera tile), narrated (through her existing voice), and guarded (every
consequential click is a canonical M0 Action, route `browser`).

Full spec, slices B0–B5, API contract, state machines, config and security:
[`LAURA-SABLE-BROWSER-OPERATOR-IMPLEMENTATION.md`](LAURA-SABLE-BROWSER-OPERATOR-IMPLEMENTATION.md).

### B0 — IMPLEMENTED (branch `claude/browser-b0`, stacked on DF0-DF1)

The presentation/operator spike, broadened from the spec's pixel-only B0 to a
full contracts-and-boundaries proof on a deterministic fake provider (report:
[`BROWSER-B0-EVALUATION.md`](BROWSER-B0-EVALUATION.md)):

- **Migration `0013_browser_sessions`**: `browser_sessions` (org + principal +
  avatar/overlay-version + meeting ref + provider + state machine + TTL +
  command seq, FORCE RLS), `browser_commands` (idempotency claims),
  `browser_presentation_tokens` (sha256-only, revocable), and the
  `due_browser_orgs` reconcile definer.
- **`backend/app/browser/`**: `provider.py` (the ONE `BrowserOperator`
  boundary), `fake_provider.py` (deterministic pages/history/failures/viewer,
  zero network), `browserbase_provider.py` (smallest real adapter,
  `ProviderUnconfigured` without flag+keys), `policy.py` (deterministic
  auto/guarded/blocked classifier + observation sanitizer + secret redaction),
  `tokens.py`, `dal.py`, `operator.py` (state machine + ownership +
  exactly-once commands + ACP write-routing), `router.py` (`/org/browser/*`
  + strict `/dashboard/browser/*` twin).
- **ACP integration**: guarded writes mint `queued_actions` rows with
  `execution_route='browser'`; BOTH approve doors execute them behind the
  existing claim with execution-time ownership/state/tool re-checks. B0
  default posture is read-only (`BROWSER_ALLOW_WRITES=false` ⇒ writes
  rejected, proven in tests).
- **Presentation**: opaque short-lived `lbt_*` tokens exchanged server-side
  for a read-only viewer; `talk.html` `browser_view`/`browser_view_hide`
  control messages (avatar shrinks to corner tile, CSS-only, flag-off
  byte-identical).
- **Deployment order**: `alembic upgrade head` (0013) →
  `BROWSER_OPERATOR_ENABLED=true` (fake provider demos work immediately) →
  B1 for the real provider (`BROWSER_REAL_PROVIDER_ENABLED` + keys).
  Rollback = flag off; migration additive-only.
- **Demo-handoff contracts** (frozen for a one-company demo integration):
  `browser/contracts.py` (`BrowserObservation`, `command_result`,
  `verify_expectation`, `clean_metadata`) + `browser/planner.py`
  (`VisualPlanner` + deterministic `ScriptedPlanner`). `page_version` bumps on
  page change; commands accept `verify=true`+`expected` so the OPERATOR (never
  the planner) decides success by re-observing; sessions carry bounded,
  non-authoritative demo-run metadata. Fixtures:
  `frontend/fixtures/browser_{states,handoff,presentation_events}.json`.
  Real-provider smoke procedure in
  [`BROWSER-B0-EVALUATION.md`](BROWSER-B0-EVALUATION.md).
- **Tests**: 38 (9 key-free + 8 contracts + 21 embedded-PG blockers, incl.
  5 adversarial-finding regressions).
- **Deferred to B1+**: real Playwright/Browserbase driving + screenshot
  perception, the real multimodal planner, live-view latency/embeddability
  measurements, meeting-lifecycle autostart, takeover, profiles, allowlists,
  token-exchange rate limiting.

**What exists to build on (summary).** `execution_route='browser'` is already
admitted by the 0009 CHECK; the additive control-message channel into
`talk.html` is shipped (`_send_avatar_control` + `handleBackendMessage`);
Recall legally iframes the avatar pages because `security.py` deliberately
sets no frame-blocking headers — so an iframe *inside* `talk.html` showing a
Browserbase live view is equally legal; the per-meeting infra lifecycle hook is
a copy of `gpu_runtime.on_session_started/ended`; narration lines ride the
existing speak queue, so the meeting's latency path gains nothing.

**Acceptance gates.** Roadmap-level: **B2** (a meeting shows a live browser
view + spoken narration with zero change to speak latency and a byte-identical
key-free demo) is the demo gate; **B5** (authenticated profiles, takeover,
retention) is the production gate.

**Risks.** Browserbase is a second per-minute meter — the meeting-bound
lifecycle plus an orphan-reconcile loop (the `/check-sessions` discipline) are
mandatory, not nice-to-have. Web pages are untrusted model input (prompt
injection) — deterministic policy checks stand between the planner and every
action. Viewer URLs and provider session ids are secrets: never logged, never
in the artifact.

---

## M6 — Reviewed learning + evaluation

**Goal.** Skills get better the way code does: through reviewed diffs and
regression tests — never through silent self-modification. Every improvement
is a proposed version bump in the Studio; every published skill has an eval
suite that gates publishing.

**What exists to build on.**

- **Durable evidence is already collected**: `logs_json` (bounded, distilled
  step log), `receipt_json`, `action_decisions` (who decided what, when, via
  which surface) — M0 rows are the raw material for outcome metrics without
  touching transcripts.
- **Offline pipeline harness** — `backend/scripts/simulate.py` + `ask.py`
  already replay meetings/questions key-free; the eval runner is the same idea
  pointed at skills.
- **The params door measures human correction**: how often approvers edit
  params before approving is a per-skill quality signal that costs nothing to
  compute.

**New tables/modules.**

| Piece | Shape |
|---|---|
| `skill_run_reviews` | thumbs/notes per run from the dashboard; FORCE RLS + grants per convention |
| Eval fixtures | recorded, **synthetic or distilled** inputs per skill (never raw transcripts) replayed by a `scripts/eval_skills.py` runner in CI; a failing fixture blocks publish |
| Learning proposals | a worker distils run evidence (edit rates, failure/blocked rates, needs_details frequency) into *proposed* skill diffs that appear in the Studio as draft versions awaiting human review — auto-apply does not exist |
| Metrics view | per skill-version: approval rate, param-edit distance, failure/`blocked`/`execution_unknown` rates — computed from durable rows alone |

**Acceptance gate.** A skill's quality metrics are computable from durable rows
with the meeting transcript already gone; a model-proposed improvement lands
only as a reviewed, published version bump; a regression fixture demonstrably
blocks a bad publish in CI.

**Risks.** Overfitting to fixtures (rotate/expand them from real anonymised
failures). PII discipline in fixtures — distillation must pass the same
review bar as `avatars/*/knowledge` (synthetic only in git). Review fatigue:
proposals must be rare and high-signal, or owners will rubber-stamp — batch
them and rank by evidence.

---

## Sequencing notes

- M1 and M2 are independent of M3+ and can land in either order; M2's
  knowledge binding gets more useful after M1.
- M3 is the spine for both M4 and the browser operator's B4 slice (guarded
  browser steps are skill write-steps with `route='browser'`).
- M5's B0–B2 (presentation + lifecycle) have no M3 dependency and can be built
  in parallel with M1/M2; B3+ want the intent/narration plumbing, B4 wants M3.
- M6 starts paying off as soon as M3 runs exist; its tables can land with M3.
