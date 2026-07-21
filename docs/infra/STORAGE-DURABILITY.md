# Storage durability, org memory must survive deploys

**Status: DECISION DOC, no code in this PR.** Written 2026-07-08 (Session 4,
CEDRIC-AVATAR-PLAN §P5 "ops"). Everything below about our own system was
verified against the code and the live AWS config, not assumed.

---

## 1. The problem, precisely

Laura's org memory is one SQLite file:

- `sessions` + `utterances` + `conversation_routes`: live-meeting state
  ([store.py](../../backend/app/store.py), survives a *process restart*),
- `artifacts`: every finished meeting's distilled artifact (meetings page,
  Cedric's `GET …/artifact`),
- `ledger_items`: the cross-meeting action/decision ledger
  ([ledger.py](../../backend/app/ledger.py)) that powers `carryover_brief`,
  the `/org/*` API (issue #48, merged), and the "AI employee that remembers"
  story Cedric now consumes.

The file lives at `backend/data/store.sqlite3` on AWS App Runner's
**ephemeral instance storage** (`_default_store_path()` falls back there
because `/var/data` doesn't exist on App Runner). The prod service
`laura-backend` auto-deploys every push to `main`. Each deploy replaces the
instance → **fresh disk → the entire org memory is wiped**. Restarts within
an instance survive; deploys don't. We deploy `main` several times a day, so
in practice the ledger never accumulates more than a day of memory. Fatal for
the product story now that Cedric (PR #55) asks "what's still open from
previous meetings?"

### Verified facts that constrain the choice

| Fact | Where verified | Why it matters |
|---|---|---|
| SQLite opened per call: `sqlite3.connect(...)`, `PRAGMA journal_mode=WAL`, 30s timeout, no long-lived connections | `store._connect()` (store.py:223) | WAL already on = Litestream's precondition; short-lived conns never block its checkpoints |
| All DB access serialized through one `threading.RLock` + writes are tiny row upserts | `store._LOCK`, `_persist_session`, `_persist_utterance` | Single-writer discipline already exists; concurrency needs are trivial |
| **Live-path reads are in-memory, not SQL.** `store.get()` / `get_by_conversation()` hit dicts loaded once at import (`_load_from_db()`) | store.py:214–230, 467–473 | A remote DB would NOT add read latency to the hot loop; reads never leave RAM |
| **Live-path writes ARE synchronous SQL on the event loop.** `session.add_utterance()` runs inside the async transcript webhook (main.py:1985); `mark_spoke()` (main.py:1478/1485/1613) and `session.integration = …` trigger `_persist_session` via the `__setattr__` hook (store.py:124) | main.py, store.py | Sub-ms on local disk. Any option that turns these into network round-trips **blocks the event loop per utterance**: latency is the product; this is the kill criterion |
| Ledger reads once per session (`carryover_brief` at start, via threadpool), writes once at finalize | ledger.py header + main.py:854, 959; org_api.py wraps all calls in `run_in_threadpool` | Ledger tolerates a slow backend; sessions/utterances don't |
| SQLite-isms in the schema/code | `AUTOINCREMENT`, `INSERT OR IGNORE`, `executescript()`, `?` placeholders, `try/except sqlite3.OperationalError` ALTER TABLE migration (store.py:279), `sqlite3.Row` | Every one needs touching for Postgres |
| Prod service is a **source-code deploy**, not the Dockerfile: managed `PYTHON_311` runtime, `StartCommand: python3 -m uvicorn …`, auto-deploy from GitHub `main` | `aws apprunner describe-service` on `laura-backend` (eu-central-1) | Litestream must *wrap* the server process; impossible to do cleanly in a managed-runtime deploy; see §2a |
| Autoscaling config `LauraCostControl`: **MinSize=1, MaxSize=1**, MaxConcurrency=80 | `aws apprunner describe-auto-scaling-configuration` | We are already pinned to one instance; and must stay there regardless of storage: `_sessions`, ws routing and `pending_messages` are process-local. Any ">1 instance" scenario breaks the app before it breaks the database |
| Single process: uvicorn with no `--workers` | StartCommand | One writer process. Litestream-safe |
| Instance role `LauraAppRunnerInstanceRole` already attached | describe-service | S3 access for Litestream is one IAM policy, no new credentials |
| Data volume is tiny. Sessions+utterances are **deleted at finalize** (`store.remove`, CASCADE); what persists is artifacts (~5–20 KB JSON each) and ledger lines (≤200 chars, capped 200/meeting) | store.py:509, ledger.py `_LIST_LIMIT` | 100 sessions/day ≈ ~20 MB/month of durable data. Storage cost is noise for every option; fixed fees and request pricing dominate |
| Transcripts (PII) sit in `utterances` while a meeting is live | store.py | Whatever replicates the DB replicates PII → private bucket, SSE, short retention, EU region |

---

## 2. The options

### (a) Litestream → S3 (streaming replication of the existing SQLite)

[Litestream](https://litestream.io/how-it-works/) tails SQLite's WAL and
continuously ships changes to S3; on boot you `litestream restore` and start
the app. v0.5.0 (Oct 2025) replaced the WAL-shipping format with LTX +
compaction; restore now reads ~a dozen files and adds point-in-time recovery
([release notes](https://fly.io/blog/litestream-v050-is-here/),
[Simon Willison's summary](https://simonwillison.net/2025/Oct/3/litestream/)).
Pin the newest 0.5.x: 0.5.0 had early-adopter bugs
([mtlynch's warning](https://mtlynch.io/notes/hold-off-on-litestream-0.5.0/)),
since fixed in the 0.5.x series.

**Compatibility with our code: near-perfect, zero Python changes.**
- WAL mode: already on (`store._connect`). Litestream requires it. ✔
- It takes over checkpointing; the app must not run `PRAGMA wal_checkpoint`: 
  we never do. Long-lived read transactions would stall checkpoints; we open
  a fresh connection per call and close immediately (`with` context). ✔
- One writer process (single uvicorn, no workers). ✔
- `LAURA_STORE_PATH` env already exists to pin the DB to a known path. ✔

**Restore-on-boot flow** (entrypoint, ~6 lines of shell):

```sh
litestream restore -if-replica-exists -if-db-not-exists "$LAURA_STORE_PATH"
exec litestream replicate -exec "python3 -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000"
```

Fresh App Runner disk → restore always runs (DB never exists); empty bucket on
first boot → `-if-replica-exists` starts clean. DB is a few MB → restore adds
well under a second to boot. `replicate -exec` supervises uvicorn and ships
changes for as long as it runs.

**The real cost: the deploy pipeline changes.** The prod service is a
managed-runtime source deploy; you can't wrap `StartCommand` around a binary
that isn't in the runtime image. Two ways out:

1. **Switch `laura-backend` to image-based App Runner (recommended).** The
   Dockerfile already exists at repo root (Ben's, python:3.12-slim); add the
   litestream binary + entrypoint. Auto-deploy then keys off ECR pushes, so a
   ~30-line GitHub Actions workflow (build → push ECR → App Runner picks it
   up) replaces today's push-to-main auto-deploy. One-time service update via
   `aws apprunner update-service`. This also converges prod with the
   container path already prepped for the GPU track.
2. *(Variant, not recommended)* keep the source deploy and have a start
   script download the litestream binary at boot. Zero CI work, but boot now
   depends on a GitHub-releases fetch; a fragile network dependency on the
   path that must never fail.

**Latency impact: none.** SQLite stays local; replication is a separate
process tailing the WAL. The per-utterance insert and `mark_spoke` upsert
stay sub-ms. This is the only option with literally zero live-path delta.

**Failure modes**
- **Rolling deploy = brief dual-writer window.** App Runner starts the new
  instance (which restores from S3) while the old one still serves and
  replicates. Writes landing on the old instance after the new one's restore
  point are lost; seconds' worth, and only if a deploy lands mid-meeting.
  Mitigation: don't deploy during meetings (already informal practice);
  sessions mid-deploy were already being killed by deploys today, so this is
  strictly better.
- **Instance dies between syncs** → lose the last sync-interval of writes
  (seconds). Acceptable: the durable assets (artifact, ledger) are written
  once at finalize.
- **>1 instance = corruption** (two Litestream writers fight over
  generations). We're pinned MaxSize=1 and the app requires that anyway;
  add a loud comment in the autoscaling config name / infra doc. If Laura
  ever needs horizontal scale, storage AND session routing must be redesigned
  together; that's the Postgres moment.
- **Backup-format lock-in:** 0.5 restores can't read pre-0.5 backups -
  irrelevant (we start fresh) but pin the version and don't mix.

**Cost** (S3 eu-central-1: $0.023/GB-mo storage, $0.005/1k PUT; durable data
~20 MB/mo, so storage ≈ $0). PUTs dominate and scale with *meeting hours*
(sync only uploads when the WAL changed; i.e. during live meetings):

| Volume | Active hrs/day | PUTs/mo @1s sync | $/mo @1s | $/mo @10s sync |
|---|---|---|---|---|
| 10 sessions/day (~1h each) | ~10 | ~1.1M | ~$5.40 | **~$0.55** |
| 100 sessions/day | ~16 (overlapping) | ~1.7M | ~$8.60 | **~$0.90** |

A 10s `sync-interval` costs us at most 10s of tail loss on a crash; fine for
this data. Call it **≤$1/month at both volumes** (plus ~$0 storage), software
free (Apache-2.0).

**Migration effort:** Dockerfile (+3 lines), new `etc/litestream.yml`, new
entrypoint script, new GitHub Actions workflow, one IAM policy on the
existing instance role, one `update-service` call. **Zero changes to
store.py, ledger.py, or any test.**

---

### (b) Managed Postgres (Neon or RDS) behind the store/ledger seam

**What actually has to change** (enumerated from the code, not hand-waved):

`store.py`, every function that touches SQL:
- `_connect()`, sqlite3 → psycopg + **a connection pool** (non-negotiable:
  today we open a connection per call; a per-call TCP+TLS+auth handshake to
  Postgres is 5–50 ms, on the per-utterance path that alone busts the
  latency budget),
- `_init_db()`, `executescript()` doesn't exist in psycopg (split into
  statements); `AUTOINCREMENT` → `GENERATED ALWAYS AS IDENTITY`; PRAGMAs go;
  the `try/except sqlite3.OperationalError` ALTER-TABLE migration (line 279)
  → `ADD COLUMN IF NOT EXISTS`,
- `_persist_session`, `_persist_utterance`, `register_conversation`,
  `mark_scheduled` (`INSERT OR IGNORE` → `ON CONFLICT DO NOTHING`),
  `save_artifact`, `list_artifacts`, `_load_from_db`, `remove`: all `?`
  placeholders → `%s`, `sqlite3.Row` → dict_row.

`ledger.py`: `_init_db`, `record_meeting`, `items`, `open_by_meeting`,
`resolve_item`, `carryover_brief`: same placeholder/identity/row treatment
(it deliberately reuses `store._connect`/`store._LOCK`, so it inherits every
change).

**The live-path problem is architectural, not syntactic.** The sync writes
listed in §1 (`add_utterance` in the transcript webhook, `mark_spoke` /
`integration=` via `__setattr__`) run **on the event loop**. Local SQLite
makes that a sub-ms sin; Postgres in-region (Neon has AWS eu-central-1; RDS
same VPC region) is 1–5 ms per round-trip *when pooled*: per utterance,
while the loop should be racing to answer. Fixing it properly means
write-behind persistence (queue + background flusher); new machinery on the
hottest, most fragile path in the product, exactly where we've spent weeks
tuning. The `run_in_threadpool` callers (org_api.py, main.py:854/959) are
fine as-is; they already assume the call may be slow. But main.py:1060–1065
(`/org/memory` debug endpoint) calls `ledger.carryover_brief` synchronously
in an async handler; one more site to fix.

Tests: `test_store`, `test_ledger`, `test_autopilot`, `test_org_api`,
`test_cedric_integration` all point `LAURA_STORE_PATH` at tmp SQLite files -
either they keep running on SQLite (now testing a different dialect than
prod runs: drift risk) or CI grows a Postgres service. Either way the
"demo runs key-free" property needs SQLite kept as the local fallback →
**dual-dialect maintenance forever**.

**Failure modes**
- Neon scale-to-zero: first query after idle pays a ~300–500 ms cold resume -
  the first meeting of the morning starts with a stall, or we pay to disable
  autosuspend.
- RDS: lives in a VPC → App Runner needs a VPC connector (today egress is
  DEFAULT/public); more moving parts, and the instance is always-on.
- \>1 App Runner instance: the *database* is fine: but the app still isn't
  (in-memory sessions). Postgres buys multi-instance readiness we cannot use
  without a session-routing redesign.

**Cost** ([Neon pricing](https://neon.com/pricing): free = 100 CU-hrs/mo +
0.5 GB; paid usage-based ~$0.106/CU-hr + $0.35/GB-mo, no monthly floor):
- 10 sessions/day: ~75 CU-hrs/mo (0.25 CU × ~10 active hrs/day) → **$0 (free
  tier)**; 100 sessions/day: ~120 CU-hrs → **~$13/mo** on Launch.
- RDS db.t4g.micro single-AZ eu-central-1 + 20 GB gp3: **~$15–16/mo flat** at
  either volume (double for Multi-AZ).

**Migration effort:** 2 core files (~18 functions), config.py + requirements
(+psycopg/pool), 1 main.py endpoint, 5 test files' strategy, plus the
write-behind layer if we're honest about latency. Days of work with
regression risk on the live path.

---

### (c) Turso / libsql (SQLite dialect, managed, embedded replica)

Turso is a managed libsql (SQLite fork). The interesting mode for us is an
**embedded replica**: a real local SQLite file for reads (zero-latency),
writes forwarded to the cloud primary and synced back. SQL dialect stays
SQLite → the schema, `AUTOINCREMENT`, `INSERT OR IGNORE` and the ALTER-TABLE
migration all survive.

**What changes:** `store._connect()` swaps `sqlite3.connect` for
`libsql.connect(path, sync_url=…, auth_token=…)` + a periodic `sync()` loop;
requirements gain the `libsql` client. Ledger inherits it. Maybe a day of
work: *if* nothing snags.

**Why it still loses:**
- **Writes become network round-trips.** Embedded replicas give local reads -
  but our reads are already in RAM (§1); our *problem* calls are writes, and
  those go to the primary (in-region ~ms, same event-loop blocking issue as
  Postgres, same write-behind escape hatch).
- The Python embedded-replica client is the least battle-tested of Turso's
  SDKs (`sqlite3` API coverage is partial. `executescript`, `Row` factory
  and the `with conn` semantics all need verification), and Turso is
  mid-rewrite of its whole engine (the 2025 "Turso Database" rewrite) -
  vendor risk on our system of record.
- It reintroduces a hard runtime dependency + auth token on a path that today
  runs key-free locally; the demo story needs a fallback branch anyway.

**Failure modes:** sync-loop lag (stale replica after restore), client bugs,
vendor pivot. >1 instance: works at the DB layer (that's Turso's pitch),
irrelevant for us (app is single-instance).

**Cost** ([Turso pricing](https://turso.tech/pricing)): free tier = 5 GB /
500M row-reads per month; covers both 10 and 100 sessions/day with room to
spare; Developer plan **$4.99/mo** if we outgrow free. **$0–5/mo.**

---

## 3. Comparison at a glance

| | (a) Litestream→S3 | (b) Neon / RDS Postgres | (c) Turso embedded replica |
|---|---|---|---|
| Live-path latency delta | **Zero** (disk stays local) | Write RTT on event loop → needs write-behind rework | Write RTT on event loop (reads stay local) |
| Code changes | **None** (Docker/CI only) | ~18 fns in 2 files + pool + write-behind + tests | `_connect` + sync loop + client risk |
| $/mo @10 sessions/day | **≤$1** | $0 (Neon free) / ~$15 (RDS) | $0 |
| $/mo @100 sessions/day | **≤$1** | ~$13 (Neon) / ~$15 (RDS) | $0–5 |
| Deploy-wipe survival | Yes (restore on boot) | Yes | Yes |
| >1 instance | Breaks (but app breaks first; pinned MaxSize=1) | DB fine, app breaks | DB fine, app breaks |
| New failure surface | Dual-writer seconds during deploy; version pinning | Cold resume (Neon) / VPC connector (RDS); dialect drift vs tests | Least-mature client; vendor rewrite |
| Key-free local demo | Unchanged | Needs SQLite fallback kept | Needs fallback branch |
| Ops prerequisite | Switch prod to image deploys (Dockerfile exists) | Managed DB + secrets (+ VPC for RDS) | Turso account + token |

---

## 4. Recommendation: (a) Litestream → S3

Latency is the product, and (a) is the only option whose live-path delta is
exactly zero. It changes no Python, keeps the key-free demo untouched, costs
≈$1/month at both target volumes, and its one real constraint, single
writer, is a constraint the application already has and enforces
(`LauraCostControl` MaxSize=1). The one honest cost, moving prod to
image-based deploys, is work we want anyway (converges with the GPU-track
container path and Ben's Dockerfile, and issue #51 is about consolidating
App Runner services regardless).

Revisit-trigger: the day Laura needs >1 backend instance (or a second
*writer* service sharing org memory), storage and session routing must be
redesigned together; that is the moment for Postgres, and the store/ledger
seam this doc enumerates in §2b is the map for it.

### Rollout (in order, each step reversible)

1. **S3 bucket** `laura-org-memory` (eu-central-1: same residency as Recall):
   Block Public Access on, SSE-S3, versioning off, lifecycle rule expiring
   noncurrent objects at 7 days. *PII note: while a meeting is live the
   replica contains transcript utterances; the short lifecycle plus the
   at-finalize row deletion keeps the durable tail distilled-only.*
2. **IAM**: attach a minimal policy (`s3:GetObject/PutObject/DeleteObject/
   ListBucket` on that bucket) to the existing `LauraAppRunnerInstanceRole`.
   Litestream picks up instance-role credentials natively; no new secrets.
3. **Repo changes** (one small PR, S1-owner files so sequence after the
   foldback): Dockerfile installs pinned litestream 0.5.x; add
   `etc/litestream.yml` (replica → `s3://laura-org-memory/store`,
   `sync-interval: 10s`); add `scripts/entrypoint.sh` with the
   restore-then-replicate flow from §2a; set `LAURA_STORE_PATH=/data/store.sqlite3`.
   Local behaviour unchanged: without the env/bucket, litestream is simply
   not in the loop (`uvicorn` direct); demo stays key-free.
4. **CI**: GitHub Actions workflow: on push to `main`: build image, push to
   ECR, App Runner auto-deploys on ECR push (preserves today's
   push-to-main → prod behaviour).
5. **Cut over `laura-backend`** to the ECR image (`aws apprunner
   update-service`), env + SSM secrets carried over verbatim. Do it in a
   no-meeting window.
6. **Verify durability end-to-end** (the actual acceptance test): run a
   session against prod, confirm ledger rows via `GET /org/actions`; push a
   trivial commit to force a deploy; confirm the same rows are still there
   after the new instance is up. Also confirm `litestream` shows in App
   Runner logs as replicating.
7. **Document**: update `docs/ARCHITECTURE_CURRENT.md` + this file's status;
   note in `laura_architecture.md` (avatars/laura/about) only if we consider
   "my memory survives deploys" part of Laura's self-description; then
   re-run `python3 backend/scripts/ingest.py laura`.
8. **Backstop** (optional, cheap): weekly `litestream snapshot` retention via
   the same config, so there's always a known-good full copy independent of
   the LTX chain.

**Rollback:** point the service back at the source-code deploy (config is
still in App Runner's history); worst case the app boots with an empty DB -
exactly today's behaviour after every deploy.

---

*Pricing sources (checked 2026-07-08):*
[Neon pricing](https://neon.com/pricing) · [Neon plans/docs](https://neon.com/docs/introduction/plans) ·
[Turso pricing](https://turso.tech/pricing) · [Turso Developer plan](https://turso.tech/blog/turso-cloud-debuts-the-new-developer-plan) ·
[Litestream v0.5 announcement](https://fly.io/blog/litestream-v050-is-here/) ·
[litestream.io; how it works](https://litestream.io/how-it-works/) ·
[mtlynch on 0.5.0 stability](https://mtlynch.io/notes/hold-off-on-litestream-0.5.0/) ·
S3 eu-central-1 list prices (storage $0.023/GB-mo, PUT $0.005/1k).
