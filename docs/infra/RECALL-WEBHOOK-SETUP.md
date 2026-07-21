# Recall webhooks & auto-finalize (owner runbook)

**What this delivers:** every meeting that ends actually gets *finalized* by
Laura; both vendors' per-minute meters stopped, the artifact built, and (for
Model A) `session.ended` handed to Cedric. Before the fix, an email/calendar
meeting could sit in `in_progress` forever because the "call is over" signal
never reached Laura.

> **This PR is code-only.** The reconciliation loop below is **on by default**
> (`RECONCILE_ENABLED=true`), so auto-finalize works after the next deploy with
> **zero config changes**. The Recall dashboard webhook (Step 2) is an optional
> latency upgrade. Read it so you know the two channels; do Step 2 only if you
> want finalize faster than the 60 s poll.

---

## The one thing to understand: Recall has TWO webhook channels

Recall does **not** deliver bot lifecycle events the way the earlier code
assumed. There are two separate channels:

| Channel | Configured where | Carries |
|---|---|---|
| **Per-bot realtime endpoint** (`recording_config.realtime_endpoints`) | in `create_bot`, per bot (`recall_client.py`) | in-call data only: `transcript.data`, `transcript.partial_data`, `participant_events.*` |
| **Account status-change webhook** (Svix-signed) | Recall **dashboard**, once per account | bot lifecycle: `bot.joining_call` … **`bot.call_ended` / `bot.done` / `bot.fatal`** |

The terminal events (`bot.done`, `bot.call_ended`, `bot.fatal`) **cannot** ride
the per-bot realtime endpoint: Recall only ever sends them to the account
webhook. The old code subscribed the per-bot endpoint and expected the terminal
event there, so it never arrived → no auto-finalize.

## How auto-finalize works now (two layers)

1. **Account webhook → `/webhooks/recall`** (fast path, needs Step 2). The
   handler already accepts terminal events: `TERMINAL` includes `bot.done`,
   `bot.call_ended`, `bot.fatal`, and it reads the short code at
   `data.data.code` (the account-webhook shape). A terminal event finalizes the
   session immediately.

2. **Reconciliation loop** (`_reconcile_sessions_loop`, **default on**). Every
   `RECONCILE_POLL_SECONDS` (default 60 s) it polls Recall for each active
   session's bot (`GET /api/v1/bot/{id}/`) and finalizes any whose latest
   `status_changes[].code` is terminal. This is the **backstop**: it recovers a
   dropped/mis-configured account webhook, and, because a `bot.fatal` can fail
   to reach the webhook at all, it is the **only guaranteed** way a
   fatally-crashed bot's Anam meter gets stopped.

Both call the same idempotent, **concurrency-guarded** `_finalize_session`
(a `_finalizing` in-flight set), so the fast path and the poll racing on the
same bot, or Recall's normal `bot.call_ended` **then** `bot.done` pair, never
double-deliver `session.ended` to Cedric.

---

## What's already set on prod (verified 2026-07-09, `laura-backend` eu-central-1)

Nothing to change for this fix to work end to end:

- `SURFACE_WEBHOOK_URL = https://www.meet-cedric.com/api/laura/events` ✓; so the
  Bug #1 fix (email/calendar summons now inherit Model A) engages on deploy.
- `SURFACE_CONTEXT_URL = https://www.meet-cedric.com/api/laura/context` ✓
- **Model A only**: the autonomous Model B execution pack (`EXECUTE_*`) was
  removed, so there's no `EXECUTE_ENABLED` flag to set; every meeting is a pure
  hand-off to Cedric.
- SSM secrets wired: `LAURA_WEBHOOK_SECRET` + `LAURA_WEBHOOK_TOKEN` (outbound HMAC
  to Cedric), `LAURA_API_TOKEN`, `LAURA_CONTEXT_TOKEN`, `RECALL_API_KEY`
  (reconcile polling), **`RECALL_WEBHOOK_SECRET`** (verifies inbound Recall
  webhooks; already present, so the account webhook in Step 2 will verify). ✓

New settings introduced by this PR; both defaulted, **no env change needed**:

- `RECONCILE_ENABLED` (default `true`)
- `RECONCILE_POLL_SECONDS` (default `60`)

---

## Step 1: Deploy (nothing else required)

Merge to `main`; App Runner auto-deploys `laura-backend`. Auto-finalize (the
reconcile loop) is live immediately. Email/calendar meetings now hand off to
Cedric because they inherit `SURFACE_*`.

## Step 2: (Optional) Point the Recall account webhook at Laura, for instant finalize

Without this, finalize still happens; just up to 60 s later via the poll. Do it
to close meetings the instant Recall reports them done.

1. Recall dashboard → **Webhooks** (the account/Svix endpoint, *not* a per-bot
   setting).
2. Add endpoint URL: `https://dhfgfe6yw6.eu-central-1.awsapprunner.com/webhooks/recall`
3. Subscribe events: **`bot.done`, `bot.call_ended`, `bot.fatal`** (the three
   terminal ones; more is harmless; the handler ignores non-terminal codes).
4. Signing secret: the endpoint's `whsec_…` must equal the SSM value at
   `/laura/prod/RECALL_WEBHOOK_SECRET`. If the dashboard generates a *new*
   secret, update the SSM parameter to match (otherwise every signed status
   event 401s and only the poll finalizes).

---

## Verify after deploy (≈2 min, PII-safe)

1. Run a real test meeting (Cedric's email or "Add people"), speak a couple of
   real lines, then leave.
2. Tail App Runner logs and look for the finalize telemetry line. **counts and
   booleans only, never transcript content**:
   ```
   [finalize] bot=<id> source=<webhook|reconcile|manual> lines=<n> orchestrated=<bool> content=<bool>
   ```
   - `source=webhook` → Step 2 is working (fast path).
   - `source=reconcile` → the account webhook isn't wired/verified; the poll
     caught it (do Step 2 if you want it faster).
   - `orchestrated=true` → `session.ended` was handed to Cedric (Model A worked).
   - `content=true` with a healthy `lines=` → the transcript accumulated (rules
     out the empty-artifact worry; `content=false`/`lines=0` means nobody spoke
     or STT captured nothing; a meeting/STT issue, not a code bug).
3. Cedric side: confirm his `/api/laura/events` receiver logged one (not two)
   `session.ended` for the meeting.

## Rollback

Set `RECONCILE_ENABLED=false` to disable the poll loop (reverts to
webhook-only finalize). Remove the Step 2 dashboard endpoint to stop the fast
path. Neither touches the live meeting path.
