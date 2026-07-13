# Org-id namespace — the deep fix (personal durable orgs)

**Status:** SPEC (design of record) — implementation follows this doc.
**Owner decision (2026-07-13):** connections are **PERSONAL for now** — every user
links *their own* Slack. **NOT** domain-shared. This diverges from
`selfserve-a-identity`'s `_resolve_domain_org`, which must be reconciled toward
"personal for now" (see [Coordination](#coordination)).
**Related:** PR #175 (`is_durable_org` guard) is the **mitigation** that keeps the
connect flow alive while this lands. Bug memory: `laura-orgid-namespace-split-brain`.

---

## 1. Problem (split-brain), in one paragraph

Two org-id namespaces live in two stores:

| | key | source |
|---|---|---|
| **Session layer (SQLite)** | `u_<hash>` (`org_id == user_id`, sha256 of email) | `store.upsert_user` / `auth.current_user` |
| **Durable control plane (Postgres)** | `uuid` (`orgs.id`, `org_connections.org_id`; RLS casts `app.current_org::uuid`) | `control_plane.ensure_user` (migration 0004) |

`auth.current_user` returns the SQLite `u_<hash>` id. Once Postgres was enabled in
prod, every durable **write** received `u_<hash>` and failed the `::uuid` cast
(`InvalidTextRepresentation`) → **503 on "Add to Slack"**. #175 gates durable writes
on `is_durable_org(org_id) = enabled() and _is_uuid(org_id)`, so personal orgs fall
back to SQLite (302) — correct as a *mitigation*, but it means personal connections
never persist durably (a redeploy that wipes SQLite un-connects them). The deep fix
gives every user a **durable uuid org** so the durable layer is actually used.

## 2. Decision — personal orgs

One durable **personal** org (uuid) per user, keyed on the user's identity
(`google_sub`/`user_id`), **not** shared by email domain. "Add to Slack" links that
personal org. `ensure_user` (migration 0004) already produces a *personal-org bundle*
and returns a resolved tenant uuid — the work is to keep it personal, capture its
return, and wire it into the runtime.

## 3. Design

### 3.1 Provision a personal durable org at login
- `auth` login callback already calls `store.upsert_user` which, when the control
  plane is configured, round-trips `control_plane.ensure_user(google_sub, email, …)`.
- Make that path **return and surface** the resolved durable **uuid** org (personal).
- **Reconcile** `ensure_user` / migration to resolve **personal** (one org per user),
  not `_resolve_domain_org` (domain-shared). Domain grouping is deferred.

### 3.2 Persist the uuid on the SQLite user row (no per-request Postgres)
- Add column **`durable_org_id TEXT NULL`** to the SQLite `users` table (`store.py`).
- At login, after `ensure_user` returns, write `durable_org_id` on the user row.
- `auth.current_user` exposes it on the user dict. **The hot path (live meetings) does
  a per-request `current_user`; it must read `durable_org_id` from SQLite only — never
  a Postgres round-trip per request.**
- **Pending state:** if `durable_org_id` is absent (control plane down at signup, or a
  not-yet-backfilled user), durable ops see a non-uuid → `is_durable_org` is False →
  #175's SQLite fallback holds (connection works, marked pending-durable).

### 3.3 Boundary translation — **Option B (recommended)** vs A
- **Option B (map at the boundary — no data migration, ~1–2 days):** SQLite stays
  keyed on `u_<hash>` (`user["org_id"]`); **durable call sites pass
  `user["durable_org_id"]`** (uuid). The two stores stay separately keyed and are
  linked per-user. `is_durable_org(uuid)` is True → durable runs; when pending it's
  the `u_<hash>` → False → SQLite. `_org_connection_rows` overlays durable rows onto
  SQLite rows by `(avatar_id, provider)` (not by org_id), so mixed keys are fine.
- **Option A (unify on uuid — heavier):** `user["org_id"]` becomes the uuid everywhere
  and **all** SQLite org-scoped rows (connections, artifacts, ledger, sessions) are
  migrated `u_<hash> → uuid`. Cleaner end-state, but a real data migration.
- **Choice: ship B now**, keep A as a later consolidation once every user is backfilled.

### 3.4 Durable call sites (Option B) — pass the resolved uuid
The sites #175 already guards (`backend/app/dashboard.py`): `begin_brain_install`
(start), `/complete` saga, `_set_connection_all`, `_org_connection_rows`,
`begin_brain_disconnect`, `tombstone_brain_install` — plus billing (`member_role`,
`org_plan`) and any durable read. Each passes `durable_org_id` for the durable half
while keeping `user["org_id"]` (`u_<hash>`) for the SQLite half. A single helper
(`auth.durable_org_id(user)` or a `user["durable_org_id"]` accessor) is the one place
that knows the mapping.

### 3.5 One-shot backfill
- For every existing SQLite user: run `ensure_user` (idempotent) to guarantee a
  personal durable org, then write `durable_org_id` on the SQLite row.
- Option B needs **no row migration** — only the per-user mapping. Idempotent and
  re-runnable; safe to run repeatedly. Ship as a management command / startup task
  guarded by a flag.

### 3.6 Test plan (the acceptance test)
End-to-end, control-plane enabled against a real/RLS test DB (`test_control_plane_pg`
harness):
1. **login** → assert a durable **uuid** org is provisioned and `durable_org_id` is
   persisted on the SQLite user.
2. **connect** ("Add to Slack" → `/start` → `/complete`) → assert an `org_connections`
   **durable** row exists keyed on the uuid, status `connected`.
3. **redeploy simulation**: wipe the SQLite store, re-`current_user` → assert the
   connection is still `connected` (served from the durable mirror), i.e. it survived.
4. **pending path**: with the durable org not yet provisioned, `/start` still returns
   302 (SQLite fallback) and never 503 — #175 still protects.

## 4. Touch points
- `backend/app/store.py` — `users.durable_org_id` column + migration + `upsert_user`/
  `get_user` read/write.
- `backend/app/auth.py` — capture `ensure_user` return at login; `current_user`
  surfaces `durable_org_id`; the `durable_org_id(user)` accessor.
- `backend/app/control_plane.py` — `ensure_user` returns the personal uuid; reconcile
  personal-vs-domain.
- `backend/app/dashboard.py` — durable call sites pass `durable_org_id` (Option B).
- backfill command + `backend/tests/test_*_pg.py` e2e.

## 5. Rollout sequence (hard order)
1. **This spec** (done).
2. Implement 3.1–3.4 + backfill (3.5) + e2e (3.6). Ship as **draft PR(s)** to session A.
3. Deploy via **session A only** (App Runner is serialized across the Laura sessions).
4. Run the backfill; verify every user has a `durable_org_id`.
5. **Only then** remove #175's SQLite-fallback branch (it becomes dead once every user
   resolves to a uuid). Until step 4 is verified, #175 stays as the safety net.

## 6. Coordination
`selfserve-a-identity` rewrites `control_plane.ensure_user` toward **domain** orgs
(`_resolve_domain_org`, `_create_personal_org`). Per the owner decision this must
resolve **personal for now**. Reconcile before/at merge: keep `_create_personal_org`
as the resolver, defer `_resolve_domain_org` behind a flag (off). Hand the merge order
to session A (billing + self-serve + this all touch `control_plane.py`/`dashboard.py`).

## 7. Risks
- **Hot-path DB call:** resolving durable org per request would add Postgres latency to
  live meetings — mitigated by caching on the SQLite row (3.2). Verify no PG call in
  `current_user`.
- **Mixed keys (Option B):** durable=uuid, SQLite=`u_<hash>`. Contained because the
  mirror merges by `(avatar_id, provider)`; audited at each call site.
- **Provisioning race:** first durable write can beat `ensure_user`; handled by the
  pending fallback (#175) — never 503.
- **Merge collision** with `selfserve-a/d` (large rewrites of the same files) — owned
  by session A's merge order.
