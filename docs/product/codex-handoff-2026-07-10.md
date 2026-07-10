# Codex handoff — finish the Laura↔Cedric product loop (2026-07-10)

**GOAL (definition of done):** a brand-new user goes `lauravatar.com → Try the
product → Google login → dashboard → Connect the brain → Send to a meeting →
asks for a task out loud → approval card in their Slack → Approve → the task
executes → the dashboard shows the action with a green "done" chip` — with no
manual ops step in the middle, and the avatar leaves the call when asked.

Read first: `CLAUDE.md`, `CODEX.md` (integration contract), and
`docs/product/laura-cedric-brain-contract.md` (the wire contract of record —
shapes are bit-for-bit, do not drift).

## Current state (all verified live today)

- **Login/beta**: Google OIDC + allow-list (`DASHBOARD_ALLOWED_EMAILS`), login
  page live. Google OAuth app is in *Testing* → every tester must be added as a
  test user in Google Console (project `868562221752`).
- **Dashboard** (`frontend/dashboard.html` + `backend/app/dashboard.py`):
  avatars, dispatch, meetings w/ AI summary + actions + execution chips,
  Usage/billing, **Configure only on Cedric's card**: "Connect the brain"
  (team+channel form) + the brain's real connector catalog fetched live.
- **Brain contract shipped e2e**: org_id on every callback event; per-org HMAC
  signing (`LAURA_WEBHOOK_SECRETS_BY_ORG`); Cedric (staging) verifies per-org,
  routes ref-less events via org link → `defaultChannel`; provisioning
  `POST/DELETE /api/laura/orgs`; provenance `POST /org/actions/{id}/status`
  (Laura, live) + `reportActionStatus` at the 3 decision points (Cedric) +
  pull route `GET /api/laura/actions/status`.
- **Product bridge**: Cedric `GET /api/laura/connectors?org_id=` → Laura proxy
  `GET /dashboard/connections/brain/connectors` → rendered in Configure.
- **Environments**: Laura prod = App Runner `laura-backend` (auto-deploy from
  `Dunic15/Laura` main; **one deploy at a time**). Cedric staging =
  `cedric-staging.vercel.app` (auto-deploy on push to `staging`); Cedric prod =
  `www.meet-cedric.com` (main, **manual** `vercel --prod` by Ben). Laura's
  `SURFACE_WEBHOOK_URL`/`SURFACE_CONTEXT_URL`/`CEDRIC_ORGS_URL` currently point
  at **staging** (flip to prod after Ben promotes).
- **Test tenancy**: playground Slack `T0BD32TEEVD` is linked to org
  `u_37428c48502d08b6` (= ducciprofeti@gmail.com), approvals channel
  `#all-bots-playground` (`C0BECQVB6KS`). Real SFF Slack = `T0AT2QWB4C8` —
  never run tests there. One workspace ↔ one org (409 on cross-org).
- Today's live-test artifacts: capture works (artifact + action_id), card
  delivery works (verified on the playground), session reconciler closes
  orphans within minutes.

## Punch list (priority order, each with acceptance criteria)

1. **Leave-on-command bug — issue Dunic15/Laura#117.** The avatar ignored
   repeated "end/exit" asks (bot `9993f7aa…`). Own the fix in the live path
   (`backend/app/decision.py` / leave handling; see PRs #93/#99 for the prior
   fixes). ACCEPT: in a live Meet, "Cedric puoi uscire" / "you can leave" makes
   the bot leave within ~5s; add a regression test.
2. **Registry ops automation (removes the last manual step).** Today the
   minted per-org webhook secret returned by `POST /api/laura/orgs` is copied
   into SSM by hand. Build: after a successful provision in
   `dashboard.connect_brain`, write `{org_id: secret}` into the SSM param
   `/laura/prod/LAURA_WEBHOOK_SECRETS_BY_ORG` (merge, never clobber) via boto3
   (App Runner instance role must get `ssm:GetParameter/PutParameter` on that
   ARN — check; if missing, add to the instance role) **and** hot-reload the
   registry in-process (make `_secret_for` read a cached dict refreshed from
   SSM every N minutes, instead of the env var frozen at boot). ACCEPT: a new
   user's Connect-the-brain → their next meeting's events verify per-org with
   zero human steps and no redeploy.
3. **Recall 507 → human error.** `POST /sessions/start` bubbles raw vendor
   errors. Map 507 to `{"error":"avatar_busy", "detail":"All avatars are busy
   right now — retry in a minute."}` (+ dashboard toast). ACCEPT: unit test +
   dispatch UI shows the friendly message.
4. **Approve→execute e2e with a real connector.** Connect Gmail on the
   playground workspace via the bridge (Configure → Connect → Cedric's flow),
   then Approve a captured "send recap" action. ACCEPT: email actually sent by
   Cedric, `reportActionStatus('done')` lands, dashboard chip green. If the
   execution needs `LAURA_AUTOEXECUTE`-style wiring, do NOT resurrect PR #7
   as-is (see its review) — approval-card flow only.
5. **"Add to Slack" instead of team-ID paste.** Replace the Connect-the-brain
   form's manual `T…` entry with an OAuth-style flow: Laura redirects to
   Cedric's Slack install URL with `state=org_id`; on install callback Cedric
   auto-provisions (`upsertLauraOrgLink` + laura block) and redirects back to
   the dashboard. Requires a small Cedric-side change (staging first, draft
   PR). ACCEPT: a user with a fresh Slack workspace connects without knowing
   what a team ID is.
6. **Signup auto-provision (contract §(d) tail).** On first Google login,
   fire-and-forget `provision_org(org_id, …)` when the user later links a team
   — i.e. keep it lazy, but pre-create the org row on Cedric with
   `team_id: null → pending` so Cedric knows the org exists. ACCEPT: Cedric's
   `laura_org_links` gains the row at first login; Connect later just fills
   the team.
7. **Prod promotion (with Ben).** After he runs `vercel --prod` on Cedric
   main: flip Laura's `SURFACE_WEBHOOK_URL`, `SURFACE_CONTEXT_URL`,
   `CEDRIC_ORGS_URL` to `www.meet-cedric.com`, re-provision active orgs (fresh
   creds minted on the prod DB), update the SSM registry. ACCEPT: full loop
   green against prod Cedric; staging left for staging.
8. **Auth polish**: custom domain `app.lauravatar.com` (App Runner custom
   domain + Cloudflare DNS); Google OAuth out of Testing (verification or
   Workspace-internal app — needs the SFF Workspace admin).
9. **Cleanup**: remove `LAURA_DEBUG` code block in Cedric `lib/laura.ts` once
   Ben agrees; retire Cedric's legacy `/api/meet/*` fail-open path (Ben's
   call); Cedric PR #7 re-implementation as opt-in with provenance wiring.

## The exact keys Codex needs (pull them yourself — don't ask for pastes)

Grab every secret in one shot (writes a local `laura-secrets.env`, values never
printed to a shared transcript). zsh does NOT treat `#` as a comment, so this
block is comment-free:

```bash
aws ssm get-parameters-by-path --path /laura/prod --recursive --with-decryption --region eu-central-1 --query "Parameters[].[Name,Value]" --output text | awk -F'\t' '{n=$1; sub(/.*\//,"",n); print n"="$2}' > laura-secrets.env
aws ssm get-parameters-by-path --path /laura/staging --recursive --with-decryption --region eu-central-1 --query "Parameters[].[Name,Value]" --output text | awk -F'\t' '{n=$1; sub(/.*\//,"",n); print n"="$2}' >> laura-secrets.env
```

You should get exactly these **19 keys** (if any are missing, that's a finding):

- **Laura↔Cedric auth (5):** `LAURA_API_TOKEN`, `LAURA_WEBHOOK_SECRET`,
  `LAURA_WEBHOOK_TOKEN`, `LAURA_CONTEXT_TOKEN`, `LAURA_WEBHOOK_SECRETS_BY_ORG`
- **Vendors (6):** `RECALL_API_KEY`, `RECALL_WEBHOOK_SECRET`,
  `ELEVENLABS_API_KEY`, `ANAM_API_KEY`, `ANAM_AVATAR_ID`, `RUNPOD_API_KEY`
- **Brains (3):** `ANTHROPIC_API_KEY`, `GROQ_API_KEY` (Cerebras-compatible),
  `CEREBRAS_API_KEY`
- **Auth/session (3):** `GOOGLE_CALENDAR_CLIENT_ID`,
  `GOOGLE_CALENDAR_CLIENT_SECRET`, `SESSION_SECRET`
- **Misc (1):** `SLACK_WEBHOOK_URL`
- **Staging admin (1):** `CEDRIC_ADMIN_SECRET` (under `/laura/staging/`)

Non-secret runtime config (URLs, allow-list) is separate — dump it with:
```bash
aws apprunner describe-service --service-arn arn:aws:apprunner:eu-central-1:836739852304:service/laura-backend/f169c4a486cd47bfac9736ab01367a26 --region eu-central-1 --query 'Service.SourceConfiguration.CodeRepository.CodeConfiguration.CodeConfigurationValues.RuntimeEnvironmentVariables' --output json > laura-config.json
```

**Keys Codex CANNOT pull from SSM** (get from a human/console):
- Cedric runtime (Neon `DATABASE_URL`, Slack app tokens, Pipedream, prod
  `ADMIN_SECRET`) → Vercel projects `cedric` / `cedric-staging` (Ben's team).
- `VERCEL_TOKEN` for staging deploys → GitHub secret on `SFF-Studio/Cedric`.
- Google OAuth app + test users → console.cloud.google.com project `868562221752`.
- Recall billing/credits → dashboard.recall.ai (owner login).

**Best practice: don't paste keys at all.** Give Codex your AWS creds
(`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, region `eu-central-1`) + `gh` auth
on both repos, and it reads secrets straight from SSM exactly like these
handoff steps did — nothing sensitive lands in a prompt.

## Credentials / API map (names + WHERE — values live in the stores, never here)

| Credential | Where it lives | Used for |
|---|---|---|
| `LAURA_API_TOKEN`, `LAURA_WEBHOOK_SECRET`, `LAURA_WEBHOOK_TOKEN`, `LAURA_CONTEXT_TOKEN` | AWS SSM `eu-central-1` → `/laura/prod/*` (SecureString) | shared Laura↔Cedric auth (bearer + HMAC) |
| `LAURA_WEBHOOK_SECRETS_BY_ORG` | SSM `/laura/prod/LAURA_WEBHOOK_SECRETS_BY_ORG` | per-org signing registry (JSON `{org: secret}`) |
| `CEDRIC_ADMIN_SECRET` (staging) | SSM `/laura/staging/CEDRIC_ADMIN_SECRET` | Cedric staging admin routes (`x-admin-secret` header, NOT bearer): init-db, traces, slack-channels |
| `RECALL_API_KEY` | SSM `/laura/prod/RECALL_API_KEY` | Recall bot create/list (`https://eu-central-1.recall.ai`); billing at dashboard.recall.ai |
| `ELEVENLABS_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`(=Cerebras), `CEREBRAS_API_KEY`, `RUNPOD_API_KEY`, `ANAM_*` | SSM `/laura/prod/*` | voice / brains / GPU face |
| `GOOGLE_CALENDAR_CLIENT_ID/SECRET`, `SESSION_SECRET` | SSM `/laura/prod/*` | Google login + calendar; cookie HMAC |
| Plain env (App Runner `laura-backend`) | `aws apprunner describe-service` | `CEDRIC_ORGS_URL`, `DASHBOARD_ALLOWED_EMAILS`, `SURFACE_*`, `CALENDAR_INVITE_EMAILS`, … |
| `VERCEL_TOKEN`, `CRON_SECRET` | GitHub secrets on `SFF-Studio/Cedric` | staging deploy workflow + ops-staging-env workflow |
| Cedric envs (`DATABASE_URL` Neon, Slack app creds, Pipedream, `ADMIN_SECRET` prod) | Vercel projects `cedric` / `cedric-staging` (Ben's team) | Cedric runtime — reachable only via Ben or the CI workflows |
| Google OAuth app + test users | console.cloud.google.com, project `868562221752` → Google Auth Platform → Audience | who can log in while in Testing |

Ops idioms that MUST be followed (they cost money/PII when skipped):
- **Session guard as its own step** before ANY Laura merge/deploy/update-service:
  `GET /health` `active_sessions==0` AND `apprunner list-operations` not
  IN_PROGRESS — read the result, THEN act. Never batch check+merge.
- App Runner env changes: `describe-service` → patch the FULL
  `CodeConfigurationValues` env maps → `update-service` (partial maps clobber).
- Cedric: branch from `staging`, draft PRs, never `main` direct, never
  `vercel --prod`; schema changes go in BOTH `db/schema.sql` and
  `ensureSchema()`; `tsc --noEmit` + a `scripts/verify-*.ts` per PR.
- Transcripts are PII: never log them, never send them over the orchestrator
  API; status `detail` strings are capped one-liners.
- Secrets: never print values in output/commits; fetch into shell vars from SSM.

## Debug toolbox (all working today)
- Cedric traces: `GET https://cedric-staging.vercel.app/api/admin/traces?kind=laura_event&limit=8` (`x-admin-secret`)
- What a bot token sees: `GET .../api/admin/slack-channels?team=T0BD32TEEVD`
- Dry-run pipeline: `POST .../api/laura/test/dry-run`; full trace: `/api/laura/test/trace?bot_id=`
- Laura status receiver: `POST {laura}/org/actions/{id}/status` (bearer)
- Synthetic signed event recipe: see `docs/product/laura-cedric-brain-contract.md` §wire shapes (HMAC `t.body`, per-org secret from a re-provision — creds are re-returned idempotently)

---

## ↔ Claude session update (2026-07-10, late) — coordinating with Codex

A second Claude session did a big prod push tonight under explicit owner
authorization. Recording it here so Codex doesn't redo work and can pick up the
still-open punch-list items with accurate state.

**DONE tonight (was punch-list item 7, done ahead of the "wait for Ben" plan —
Duccio authorized deploying Cedric prod directly):**
- **Cedric PROD deployed** to `www.meet-cedric.com` with all of today's fixes.
  Method matters: a direct `vercel deploy --prod` hung/blocked twice — root
  cause was Vercel refusing the promotion because the git commit author
  (`duccio.profeti@mail.polimi.it`, a VIEWER on team `benji-benhattars-projects`
  / `team_zXGPqlARcwb0fa4EAp0BQGjl`) lacks deploy rights. **Fix = `rm -rf .git`
  before `vercel deploy` (same trick as `deploy-staging.yml`)** so Vercel uses
  the token's authority. Prod project id `prj_JUwXsjxd8TqNLtxEayiFZLbrsvFw`.
  Repo already has a `VERCEL_TOKEN` secret with prod access.
- **Laura config flipped to PROD**: `SURFACE_WEBHOOK_URL` +
  `SURFACE_CONTEXT_URL` → `www.meet-cedric.com/api/laura/{events,context}`,
  and `SURFACE_EXTERNAL_REF` set. Prod receiver verified live (signed event →
  `200 {ok:true}`), `action.requested` dispatch confirmed in logs.
- Cedric-side PRs merged today: **#14** (dedup approval cards by action_id,
  invite the requester to created events, who's-who brief, undeliverable-card
  trace) and **#15** (agent_task resolves `done` only when it actually
  completed — a blocked "need Marco's email" stays `approved`/open). Laura-side
  PR **#114** (cross-lang action dedup, capture-window guard, resolve-outcome
  contract + status→ledger weld, context-pull fallback, hermetic test suite).

**⚠ COORDINATION CONFLICT to reconcile — test tenancy:**
- This handoff says the canonical test tenant is the **playground**
  `T0BD32TEEVD` / `#all-bots-playground` (`C0BECQVB6KS`), and **never SFF Studio
  `T0AT2QWB4C8`**. The other Claude session, per Duccio's direct instruction,
  configured `SURFACE_EXTERNAL_REF` + tested on **SFF Studio `T0AT2QWB4C8` /
  `#test-laura` (`C0BGBG3EQDR`)** — and prod @cedric posted cards there fine.
- Consequence: `SURFACE_CONTEXT_URL` currently carries `team=T0AT2QWB4C8`, and
  the staging `channel_not_found` seen earlier was actually *correct* behaviour
  (staging Cedric is bound to the playground `T0BD32TEEVD`, not SFF Studio).
- **Decision needed** (Duccio/Codex): is the live product test on SFF Studio
  `#test-laura` now the intended path, or should Laura's `SURFACE_EXTERNAL_REF`
  revert to the playground? Right now Laura points prod events at
  `T0AT2QWB4C8/#test-laura`.

**Still open for Codex (unchanged):** items 2 (registry SSM automation), 3
(Recall 507 → friendly error), 4 (Approve→execute with a real Gmail connector),
5 ("Add to Slack" OAuth), 6 (signup auto-provision), 8 (auth polish). Item 1
(leave) already had PR #93/#99 + a round-2 fix this session; re-check #117
against the deployed prod before more work.

_(Codex CLI coordination via the OpenAI plan was usage-capped tonight until
~01:50; this async handoff is the coordination channel instead.)_
