# Native Integrations Plan — Laura owns Calendar + Gmail; Cedric becomes optional

_Owner direction, 2026-07-14 (post-demo, advisor Christian). Product lead spec. Do not commit code from this doc._

## Thesis

Laura should **execute its own approved actions natively** — create Google Calendar events and
send Gmail — using an OAuth token Laura already knows how to obtain, so a new user is self-contained
after one Google consent screen. **Cedric flips from the mandatory execution brain to an optional
"power" add-on** (Slack orchestration + the long tail of tools) that a user turns on in settings.
This directly answers the demo feedback (onboarding needs no second signup + no separate LLM key)
and retires last night's failure mode: brokering execution through Cedric/Pipedream (workspace-blob
clobbering, stale serverless reads, org→workspace 404s). We already run a proven Laura-owned Google
OAuth flow (`/oauth/google/connect`); native execution is the same flow with two more scopes, not a
new subsystem.

## Architecture: where the native executor slots in

Today an approved action leaves Laura **only** through Cedric: finalize builds an `integration`
(`cedric/integration.py::build_integration` / `default_integration`, gated on `SURFACE_WEBHOOK_URL`),
`deliver_ended`/`notify_action_requested` POST it, and Cedric does the Slack card + tool execution.
When `SURFACE_WEBHOOK_URL` is unset, sessions fall to native "autopilot" (`autopilot.maybe_deliver` →
`actions.send_email` SendGrid + `actions.post_to_slack`) — but that's a fire-and-forget whole-artifact
dump with **no approval gate and no calendar/Gmail action**.

The new seam is a thin **executor** that turns an *approved* ledger action into a real Google API call:

```
finalize / approval  ──►  action (ledger row, stable action_id)
                              │
              execution_mode? ├─ "native"  ─► backend/app/executor.py
              (per-org setting)│               dispatch by action.type:
                               │                 calendar.create_event ─► google_client.create_event()
                               │                 email.send            ─► google_client.gmail_send()
                               │               writes provenance back → ledger row + artifact
                               │
                               └─ "cedric"  ─► existing cedric/integration.deliver_ended (UNCHANGED)
```

- **New:** `backend/app/executor.py` (dispatch + provenance write) and `google_client.py` (Calendar
  `events.insert`, Gmail `messages.send`) reusing the OAuth token from the existing callback.
- **OAuth reuse + gap:** `/oauth/google/callback` already yields a refresh token, but it is handed
  straight to Recall's `create_calendar` and **never persisted**, and the scopes are read-only
  (`calendar.events.readonly`, `gmail.readonly`). Native execution needs: (a) add `calendar.events`
  and `gmail.send` to `GOOGLE_CALENDAR_SCOPES` in `main.py`; (b) persist the refresh token per org in
  `store.py` (new `org_oauth` table) so the executor can mint access tokens at finalize.
- **Ledger is the state machine.** Extend `ledger_items.status` from `open|done|noted` to add
  `approved` and `executed`, and add provenance columns (`provenance_url`, `provenance_ref`) so a
  "Done" receipt carries the actual Calendar event link / Gmail message id — closing Cedric's opaque-
  "Done" gap. The stable `action_id` already threads webhook → artifact → ledger, so nothing about
  correlation changes.

### Where approvals live when Cedric is OFF

**Dashboard-native, email-notified. No new Laura Slack app in this cut.**
- The **dashboard** is the approval surface: each finalized action renders as an approval card (reuse
  the existing dashboard action rows in `dashboard.py`); Approve calls the *existing* resolve endpoint
  `POST /org/actions/{action_id}/resolve` (`org_api.py` → `ledger.resolve_by_action_id`), which flips
  the row to `approved` and enqueues the executor.
- **Notification** rides the native channel we already have: `actions.send_email` sends an "N actions
  await your approval" email linking to the dashboard. No SendGrid key set → falls back to the
  dashboard badge only (zero-key demo stays intact).
- A Laura-owned minimal Slack app for approvals is explicitly **deferred** — that is exactly the
  Slack-orchestration value Cedric already provides, so building it natively now is scope creep and
  re-opens the surface we're trying to simplify away.

## Roadmap — Now / Next / Later (S/M/L effort)

### Now (owner's own Google account; prove the loop end-to-end)
- **[M] Native Google executor for the owner.** Add `calendar.events` + `gmail.send` scopes; persist
  the owner's refresh token; `executor.py` + `google_client.py` execute `calendar.create_event` and
  `email.send` on approval. Off by default behind `NATIVE_EXECUTOR=true` (mirrors the autopilot
  opt-in) so the zero-key demo and existing deploys are untouched. Seams: `main.py` scopes +
  `/oauth/google/callback`, `store.py` (org_oauth), `executor.py`, `google_client.py`, `ledger.py`
  (status + provenance).
- **[S] Cedric add-on toggle in settings.** A single `execution_mode = native | cedric` setting
  (Now: one owner, so a settings flag / env is enough) that chooses the branch above. Structurally
  this promotes today's global `SURFACE_WEBHOOK_URL` gate into an explicit user choice. Seams:
  `config.py`, `dashboard.py` settings panel, the branch in finalize/`cedric/integration.py`.

### Next (multi-user + trustworthy receipts + a simpler dashboard)
- **[L] Per-org OAuth + token storage.** Every user connects their own Google account; executor picks
  the token by the session's org. Note the known `org_id` namespace split-brain (`u_<hash>` vs uuid) —
  key `org_oauth` on the same durable-org id the ledger/resolve path uses; do not introduce a third id.
- **[M] Provenance-rich "Done" receipts.** Approval → executed writes the Calendar event URL / Gmail
  thread id to the ledger row and surfaces it as a clickable receipt in the dashboard and the
  session.ended artifact — the thing Cedric's "Done" never showed.
- **[M] Dashboard simplification (name what to cut).** Cut: the cost-estimate line, the raw
  provider/model badges, and the non-actionable meeting metadata columns. Keep and make primary: the
  meeting list, the **approval queue**, and per-action **status + receipt**. The dashboard's one job
  becomes "approve what Laura wants to do, and see what she did."

### Later (narrow to a niche — the second demo ask)
Pick ONE to run a positioning experiment; all three already have assets in-repo:
- **VC / micro-fund back-office (SFF avatar already built).** Deal-flow + diligence calls; native
  actions = schedule the follow-up founder call + send the intro/pass email. Warmest because the
  knowledge pack (41 SFF portfolio cos) exists.
- **Founder / EA "clone yourself" personal assistant (`avatars/duccio` exists).** Single-user wedge
  the Now slice already serves — Laura schedules and emails on your behalf from your own Google
  account. Lowest onboarding friction, matches the self-contained thesis.
- **HR / recruiting Italy (niche4 GTM + Italian meeting-tracking already scoped).** Interview debriefs;
  native actions = schedule next-round + send candidate follow-up. Heaviest lift (Italian MeetingState),
  so a *later* not a *first*.

_Recommendation: run the niche experiment on VC or Founder-EA, where the avatar + knowledge already exist._

## Risks + what NOT to break

- **Live-meeting contract is untouched.** No change to `ws/<id>` / SSE+poll, `{type:"speak"}`,
  `recall_client`/`anam_client` signatures, or the live latency path. The executor runs at
  finalize/approval, never on the transcript→speak path.
- **Existing Cedric orgs keep working unchanged.** Any deploy with `SURFACE_WEBHOOK_URL` set stays in
  Model A (`execution_mode=cedric` is the default there); native is additive and opt-in.
- **Zero-key demo intact.** `NATIVE_EXECUTOR` defaults off; no Google key → dashboard badge only; the
  offline stub demo is never asked for a key.
- **PII discipline.** The executor sends only already-distilled action text (owner/due/subject/body
  from the artifact) — never transcript content — and logs no transcript, consistent with hard
  constraint 6 and the guard hook.
- **Scope + token security.** Adding write scopes means Google verification/consent review and storing
  a refresh token — persist encrypted (KMS/SecureString, as the Cedric bearer already is), never in git.
- **Attribution bug to fix in Now.** Auto-join (Gmail watcher / calendar) sessions currently attribute
  to the demo org; native per-org execution must resolve the session's real org before it mints tokens,
  or a user's email/calendar action could fire from the wrong account.
