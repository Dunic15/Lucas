# Access governance — spec

Owner ask 2026-07-23: *"identità Laura separata per ogni azienda; credenziali
dedicate; knowledge deny-by-default verificata dal backend; nessun accesso
permanente ottenuto partecipando a un meeting; un amministratore che concede e
revoca; audit log di letture, modifiche e azioni; isolamento completo tra
organizzazioni."*

Three of those shipped on 2026-07-23 (#401 per-org avatar switches, #402
per-tenant credentials, #403 action doors scoped to attendance). This document
specifies what is left, in the order it should be built. It is a spec, not a
plan of record — the sizing is honest, including where it is uncomfortable.

---

## Where we actually are

| Requirement | State | Evidence |
|---|---|---|
| Org isolation | **Strong** on Postgres | FORCE RLS on every org table, `app.current_org` set transaction-local, runtime role is NOSUPERUSER/NOBYPASSRLS, cross-org reads only via `laura_private.*` SECURITY DEFINER returning org ids |
| Org isolation | **Application-only** on SQLite | The live runtime store has no RLS; isolation is `WHERE org_id = ?` discipline. `store.get(bot_id)` takes no org at all |
| Per-company identity | **Done** | `avatar_capabilities` / `avatar_brain_mode` are org-keyed (#401) |
| Dedicated credentials | **Done** | Google per-org *and* per-user; Asana/Pipedream per-org; the deployment-wide `ASANA_TOKEN` / `SLACK_WEBHOOK_URL` now serve only the deployment's own org (#402) |
| Action authorization | **Partly done** | All four action doors now require attendance (#403). Roles are still not consulted |
| Knowledge access | **Org-level only** | Any member can read, upload, reassign and **delete** any source. Retrieval narrows per *avatar*, never per user |
| Access is revocable | **No** | Access is *derived*, permanently, from artifact text |
| Admin grant/revoke | **No such surface** | `memberships.role` is written only by auto-provisioning at login |
| Audit log | **Writer implemented; admin reader pending** | Bounded metadata-only queue writes tenant-scoped action/meeting reads and canonical action decisions; runtime has INSERT only |

---

## 1. Audit log — make the table real

**Implementation status:** the non-blocking writer and first sensitive call sites are implemented in the audit-security-events branch. The admin-only read/export surface remains coupled to the role helper in §3.

**Why first:** it is the only item that is purely additive. Nothing changes
behaviour, so it can ship without a migration window, and every later change
becomes reviewable because it is recorded.

**Write on:** meeting/artifact read, transcript reveal, knowledge document read
and search, action read/approve/reject/param-edit, credential connect and
disconnect, capability and preference toggles, membership changes (once §3
exists).

**Row shape** (already in the schema): `org_id`, `actor_user_id`, `action`,
`target`, `ts`. Nothing else — **never** transcript text, decision text,
document content, or a credential. `target` is an id (`bot_id`, `action_id`,
`source_id`), never a title.

**Non-negotiables**

* Off the live meeting path. A write failure must never break the request:
  best-effort, wrapped, and it may drop rows before it may add latency.
* Append-only is already enforced by grants (`REVOKE UPDATE, DELETE`); do not
  "fix" that when the table grows.
* Reads are the point. Writes are already partly covered by
  `action_decisions`; **who looked** is what nobody can answer today.
* Retention: a documented window with a purge job, so it doesn't become an
  unbounded PII-adjacent store. Start at 180 days.

**Surface:** an `/org/audit` read for admins (§3) and an export. Without a
reader it is compliance theatre.

**Size:** ~1 focused PR for the writer + call sites; a second for the read
surface.

---

## 2. Knowledge: deny-by-default per person

Today `_dash_org` is the whole check: any logged-in member of the org can list
sources, read document excerpts, reassign and delete them. Retrieval filters by
`avatar_id` — and with an empty `avatar_id`, `keyword_search` returns the org's
entire published corpus.

**Target:** a source carries an access rule; a user sees a document only if the
rule admits them. `df_acl_entries` and `allowed_principals` already exist,
unpopulated, for exactly this.

**Rules**

* Default **deny** for a new source unless it is explicitly org-wide.
* Enforcement in the **DAL**, next to the org predicate — never in the route,
  never in the UI. "Verified by the backend" means the query cannot return a
  row the caller may not see.
* `keyword_search` with no `avatar_id` must narrow to the caller's permitted
  set, not fall through to everything.
* Retrieval during a meeting resolves against the **avatar's** grant, not the
  attendees' — an avatar must not become a way to read what a person present
  could not read directly.

**Uncomfortable part:** this changes what existing orgs can see. It needs a
migration decision (grandfather current sources as org-wide, then deny by
default for new ones) and a deliberate owner sign-off.

**Size:** 2 PRs (model + DAL enforcement; then UI to set the rule).

---

## 3. Membership administration + revocable access

This is the big one, and §4 depends on it.

**Problem today.** There is no route to invite, remove, suspend or re-role
anyone. A verified corporate domain auto-joins new logins as `member` with no
invitation and no approval. Roles (`owner|admin|member|billing`) exist in the
schema and are enforced in exactly two places (billing checkout, avatar-studio
dashboard writes); every other mutation treats "logged in" as "authorized" —
including connecting and **disconnecting the org's credentials**.

**Target**

* `POST /org/members` (invite), `DELETE /org/members/{id}` (remove),
  `PATCH /org/members/{id}` (role). Admin/owner only, audited (§1).
* Auto-join on a verified domain becomes **request access**, not join.
* A single `require_role(...)` dependency applied to every mutating route.
  The audit found the machine door for avatar overlays bypassing the admin
  gate its dashboard twin enforces — one shared helper is how that stops
  recurring.
* Removing a member revokes access immediately, everywhere.

**Size:** 2–3 PRs plus a small admin UI. Not a night's work.

---

## 4. Access to a meeting: granted, not inferred

**Problem today.** `_user_attended` decides visibility, and it:

1. **fails open** — no `principal_id` *and* no transcript speakers ⇒ visible to
   the whole org;
2. matches on **first name or a 4-character prefix** of an **ASR display
   name** — an unauthenticated, self-chosen string;
3. is **permanent and irrevocable**, because it is recomputed from immutable
   artifact text on every read;
4. degrades on restart: `principal_id` is in-memory only, so a mid-meeting
   restart drops every meeting to the fuzzy path.

**Target:** attendance becomes a **recorded grant**, not an inference.

* At finalize, write explicit `meeting_participants` rows: authenticated user
  ids where known (dispatcher, calendar invitees, logged-in joiners), each with
  a source (`dispatched` / `invited` / `spoke`).
* Visibility reads those rows. Name matching becomes a *hint for linking* at
  write time, never an authorization decision at read time.
* Fail **closed**: unknown ⇒ not visible, with an admin able to grant.
* Admins (§3) can grant and revoke per meeting; grants can expire.
* Guests get access to the meeting they attended, and it can be withdrawn.

**Ordering note.** Doing §4 before §3 produces a system where access can be
lost but not restored — so §3 lands first, or they land together.

**Size:** 2 PRs (write the grants + read from them behind a flag; then flip
the default and remove the inference).

---

## Recommended order

1. **Audit log writer** — additive, unblocks trust in everything below.
2. **`require_role` + membership administration** — the missing authority.
3. **Meeting access as recorded grants** — needs §2 to be revocable.
4. **Knowledge per-principal ACLs** — the largest behaviour change; do it when
   there is an admin who can fix what it locks.

## What this is not

It is not "hardening". Items 2–4 turn a single-tenant product with tenant
tagging into a platform with an authority model. The current defaults hide
this: `shared_domain_orgs` is **off**, so nearly every org is one person and
the intra-org holes are latent. They become live the day a real company puts
five people in one org — i.e. the day this is worth selling.
