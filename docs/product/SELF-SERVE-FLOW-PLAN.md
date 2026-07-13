# Self-serve flow — plan & coordination (email → connect tools → 15 min free)

Target: a random user lands, signs in with their email, connects their own tools
(calendar + the Cedric brain), and gets 15 free avatar-minutes before paying.

This is the reference plan to diff against the Codex build and to hand Ben his part.
Grounded in the code as of `main@d5fb591` (2026-07-13).

## The one thing that decides whether this actually works

**All three pillars of this flow require the durable, per-org store (Supabase
Postgres + RLS + per-org connections). None of them work on today's ephemeral,
single-global-account model.** Verify Codex is building on this foundation first —
if it's built on the current SQLite/global-account model it will look done but fail
for real users:

- **Per-user tool connections** — today calendar/Gmail are ONE global platform
  Google account; `org_connections` has the `gmail`/`calendar`/`slack` provider slots
  but nothing writes them. A random user must connect *their own* account, stored
  under *their* `org_id`.
- **The 15-minute trial** — usage must persist. On the ephemeral store it resets on
  every `main` push (redeploy), so a user gets unlimited free by waiting for a deploy.
- **Tenant isolation** — auto-join currently stamps `demo_org`, visible to every
  logged-in user (the audited cross-tenant leak). Real users need real per-org rows +
  RLS.

So: **Supabase + Alembic `0001` (RLS, already validated in #146) + `LAURA_STORE_PATH`
→ Postgres is the prerequisite for the whole flow.** 👤 Duccio.

## The flow, step by step (what to build)

| # | Step (user sees) | State today | Gap to build | Owner |
|---|---|---|---|---|
| 1 | **Sign in with email** ("Continue with Google" — verified email in one click) | Google Sign-In exists (`auth.py`), signed cookie, `org_id == user_id` personal org | **Publish the Google OAuth app** (it's in Testing → random users hit "Access blocked"). Drop the private-beta allowlist gate for GA (or keep as waitlist). | 👤 Duccio (publish) + Codex (gate) |
| 2 | **Their workspace is created** | `upsert_user` → personal org on first login | Nothing — personal-org model already fits one-user-per-org | ✅ have |
| 3 | **Connect Calendar** (Laura auto-joins their meetings) | ONE global Google account auto-joins; no per-user connect | **Per-user Google Calendar OAuth** → store refresh token + `org_connection(org_id,'calendar')`; the calendar sync/webhook must resolve the bot's org from the *connection that produced the event*, not `demo_org` | Codex (needs Supabase) |
| 4 | **Connect the Brain (Cedric)** — same screen | `/dashboard/connections/brain/slack/{start,complete}` flow EXISTS; `#147` made `/complete` accept a shared org | Wire it into the connect step; Cedric side mints a **per-workspace** secret+token and posts back to `/complete` | 🧑‍💻 Ben (Cedric side) + Codex (wire) |
| 5 | **15 free minutes, then upgrade** | per-minute meter exists (start/end + hardened `leave_call` cutoff, #148); NO trial/quota | Build the trial (below) | Codex (needs Supabase for persistence) |

## The 15-minute trial — concrete mechanism

Leverages the meter hardening from #148 (verified `leave_call` cutoff) and the
existing `_reconcile_once` 60s loop:

1. **Budget**: `orgs.plan ∈ {free, paid}` already exists. Add a free budget
   (`settings.free_trial_seconds = 900`) and a **persisted** per-org usage counter.
   Cheapest source of truth: `SELECT sum(duration_seconds) FROM artifacts WHERE
   org_id=?` (duration is already stored per meeting) — **but only correct once the
   store is durable** (else it resets on redeploy). On the durable store this needs
   no new table.
2. **Enforce at start** (`/sessions/start`): if `plan=='free'` and `used >= budget`
   → reject with `402 trial_exhausted` + an upgrade link. (Don't even dispatch a bot.)
3. **Enforce mid-call** (`_reconcile_once`, every ~60s): for each active session, if
   `plan=='free'` and `used + elapsed(now - session.created_at) >= budget` →
   `leave_call` (the hardened cutoff) + finalize + flag `trial_ended`. Graceful stop
   at 15 min without stranding the meter.
4. **Warn live** (optional): Laura says one line near the cap ("we're at the end of
   the free trial — upgrade to keep going").
5. **Dashboard**: "X of 15 free minutes used" tile + an Upgrade CTA (the payment
   link/Stripe is a 👤 Duccio business decision, out of scope for the mechanism).

## 🧑‍💻 What Ben needs to do (the brain in the same connect step)

Today Laura→Cedric uses ONE global bearer and Cedric's `_org_for` hard-returns
`demo_org` — so Cedric can't tell workspaces apart. For "connect the brain = connect
their workspace," Ben's Cedric app must:

1. **Per-workspace install → mint a secret.** When a user clicks "Connect Cedric" in
   Laura, they hit Cedric's Slack OAuth (Ben's app). On authorize, Cedric mints a
   **per-workspace webhook secret + bearer** and POSTs it back to Laura's existing
   `POST /dashboard/connections/brain/slack/complete` with `{org_id, team_id,
   webhook_secret}`. (Laura's side is already built + accepts a shared org since #147.)
2. **Per-workspace token → org resolution.** Cedric must resolve which org/workspace a
   request belongs to *from that per-workspace token* (replace the single global bearer
   + the `_org_for → demo_org` stub). So Cedric-initiated dispatch and action callbacks
   carry the right `org_id`.
3. **Slack Interactivity Request URL** set to the action-approval endpoint (for the
   approve/deny buttons on captured actions).
4. **(If follow-ups send via Cedric)** Gmail-send wiring on the Cedric side.

Net: the onboarding "Connect" screen has **Calendar** (Laura auto-join) and **Cedric**
(Ben's per-workspace Slack OAuth), both writing `org_connections` under the user's org.

## Diff-against-Codex checklist (align on these decisions)

1. **Foundation**: is it on Supabase/Postgres + RLS + per-org connections, or the
   ephemeral/global model? (If the latter — stop; it won't work for real users.)
2. **Auth**: Google Sign-In (published) vs email magic-link. (Recommend Google — reuses
   auth + the tools are Google anyway; "just put email" = one-click Continue-with-Google.)
3. **Connection→org resolution**: how does an auto-joined meeting learn its owning org?
   (Must be the connecting user's org via `org_connections`, never attendee email — spoof
   hole, MULTI-TENANCY §3.1.)
4. **Trial storage + reset semantics**: per-org persisted seconds; one-time 15 min
   (not per-session, not resettable by redeploy).
5. **Trial enforcement points**: start (402) + `_reconcile_once` mid-call cutoff via
   `leave_call`. Same as above?
6. **Brain step**: does "Connect" provision the per-workspace Cedric secret (Ben) in the
   same flow, writing `org_connections(org_id,'cedric-brain','connected')`?
