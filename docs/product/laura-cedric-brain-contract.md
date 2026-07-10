# Laura ↔ Cedric brain contract — v1 (grounded recon, 2026-07-10)

Source: 6-agent recon of `SFF-Studio/Cedric` @ main (synthesis in session log).
Cedric = Next.js 14 / Vercel / Neon Postgres, tenant = **Slack team_id**, no ORM,
schema dual-maintained (`db/schema.sql` + `ensureSchema()` in `lib/db.ts`).

## What already exists (don't rebuild)

| Piece | Where | Status |
|---|---|---|
| Booking Cedric→Laura (`/sessions/start`, Bearer `LAURA_API_TOKEN`, `external_ref {team, slack_channel, requested_by}`) | `lib/laura.ts:185-351` | live |
| Webhooks Laura→Cedric (`POST /api/laura/events`: `session.status/action.requested/session.ended`; Bearer + HMAC `X-Laura-Signature`, 300s replay, fail-closed) | `lib/laura.ts:68-148`, `app/api/laura/events/route.ts` | live |
| Pre-meeting context pull (`GET /api/laura/context`) | PR #8 | open |
| Auto-execute live-requested actions (cap 5, dedup, fallback card) | PR #7 | open |
| Undeliverable-event tracing | PR #11 (from our side, today) | open |
| Success-only resolve back to Laura (`POST /org/actions/{id}/resolve`) | `lib/meetFollowup.ts:479-487` | live |

**The `external_ref` echo is the entire tenancy mechanism today** — single global
`LAURA_*` credentials; the payload, not the credential, selects the tenant. Events
with no `team`+`slack_channel` are silently dropped (PR #11 adds tracing).

## The 4 new pieces (contract of record)

### 1. Per-org registry + auth (the real "Connect the brain")
- `Workspace.laura?: {orgId, apiToken?, webhookSecret, webhookToken, avatarId?, defaultChannel?}`
  on the workspace jsonb blob (follows `google.refreshToken` precedent).
- Global lookup `laura_org_links (org_id PK, team_id)` — added in BOTH `db/schema.sql`
  and `ensureSchema()` (house idiom, precedent: `integration_skills`).
- Parameterize `verifyLauraBearer`/`verifyLauraSignature` to `(secret, ...)` with env
  fallback → bit-identical behavior for the current global credential (Cedric = client #1).
- Fixes dropped events: `handleLauraEvent` falls back to `resolveWorkspaceByLauraOrg(org_id)`
  when `external_ref` is absent.
- Matches what Cedric's own `docs/External/Laura-API.md:105-108` already calls for.

### 2. Org provisioning (Laura signup → Cedric counterpart)
- `POST /api/laura/orgs` (auth: global `LAURA_API_TOKEN` pattern) with
  `{org_id, org_name, team_id?, default_slack_channel?, avatar_id?}`.
- If workspace exists → MERGE `laura` block (never rebuild — hard house rule), mint
  per-org `webhookToken/Secret` (crypto.randomBytes), insert link row, return creds ONCE.
- No `team_id` → pending/claim state (Cedric's tenant IS a Slack workspace; install first).
- `DELETE /api/laura/orgs/[orgId]` for disconnect.

### 3. Status back to Laura (dashboard provenance)
- Push: `resolveAction` → `reportActionStatus(actionId, 'proposed|approved|rejected|done|failed')`
  → `POST {LAURA_API_URL}/org/actions/{id}/status`, called at the 3 existing decision
  points (proposal creation, Approve/Reject click, execution result). Best-effort.
- Pull fallback: `GET /api/laura/actions/status?bot_id|action_id` promoted from the
  existing `test/trace` route, gated by `verifyLauraContextBearer`.
- **Laura side (this repo): add `/org/actions/{id}/status` endpoint + store per-action
  status for the dashboard.**

### 4. Laura-side webhook hardening (land FIRST or simultaneously)
- Send `org_id` in every event, a **stable `at`**, and a **per-action `action_id`** —
  today Cedric's dedup key `(event,bot_id,at)` can swallow/double-fire.
- Per-client key registry on our side (which secret to sign with per org).

## Build order
1. **Laura repo:** webhook hardening (#4) + `/org/actions/{id}/status` (#3-receiver).
2. **Cedric branch off `staging`, draft PRs:** registry+auth (#1) → provisioning (#2)
   → status push/pull (#3). All additive; verify scripts + `tsc --noEmit`; dry-run
   harness (`/api/laura/test/dry-run`) for e2e without sends.
3. **Dashboard:** "Connect the brain" in the avatar Configure tab = UI over #2.

## Wire shapes (bit-for-bit — matches what Laura ships as of PR #109/#111)

### Laura → Cedric: event payloads (already live from PR #109)
Every callback event now carries `org_id` (`""` for service starts). Signing:
`X-Laura-Signature: t=<unix>,v1=hmac_sha256(secret, "<t>.<raw_body>")` where
`secret = LAURA_WEBHOOK_SECRETS_BY_ORG[org_id]` if present else `LAURA_WEBHOOK_SECRET`.

```jsonc
// action.requested (unchanged fields omitted)
{ "event": "action.requested", "bot_id": "…", "action_id": "16-hex",
  "org_id": "u_abc123…", "external_ref": {…}, "action": "…", "owner": "…",
  "due": "…", "at": "ISO-8601 UTC" }
// session.status: + "org_id"   // session.ended: + "org_id"
```
`at` is stable across retries (payload built once). `action_id` is stable
between the live event and the same action inside `session.ended`'s
`artifact.actions[]` — dedupe/idempotency key on Cedric's side should be
`action_id` (not `(event,bot_id,at)`).

### Cedric → Laura: execution status (receiver LIVE on Laura main, PR #109)
```
POST {LAURA_API_URL}/org/actions/{action_id}/status
Authorization: Bearer LAURA_API_TOKEN        // same gate as /org/actions/{id}/resolve
Body:     {"status": "proposed|approved|rejected|done|failed", "detail": "≤300 chars, optional"}
200 OK:   {"recorded": true, "action_id": "…", "status": "…"}
400:      {"error": "status must be one of […]"} | {"error": "invalid JSON body"}
401:      bearer missing/wrong
```
Semantics: upsert latest-wins; `done` ALSO closes the ledger item (identical
effect to `/resolve` — call either, not both). `detail` is a distilled
one-liner (card link, error class), never transcript. Call it at the three
decision points: proposal created → `proposed`; Approve/Reject click →
`approved`/`rejected`; execution result → `done`/`failed`.

### Laura → Cedric: org provisioning (what Laura ALREADY sends, PR #111)
`cedric.provision_org` POSTs to `CEDRIC_ORGS_URL` (= your `/api/laura/orgs`):
```
POST /api/laura/orgs
Authorization: Bearer CEDRIC_ORGS_TOKEN      // your LAURA_API_TOKEN-style deployment credential
Body: { "org_id": "u_abc123…",               // Laura org (== user_id today)
        "team_id": "T0123ABCD",              // Slack workspace = Cedric tenant
        "default_slack_channel": "#growth",  // may be ""
        "avatar_id": "cedric" }              // may be ""
2xx  → Laura marks the connection "connected"
non-2xx / unreachable → stays "pending" (owner can retry from the dashboard)
```
Response body: your call — Laura v1 reads ONLY the status code. If you mint
per-org `webhookToken`/`webhookSecret`, return them once; ops copies them into
Laura's `LAURA_WEBHOOK_SECRETS_BY_ORG` env (Laura never stores them in its DB).
`DELETE /api/laura/orgs/{org_id}` mirrors it (Laura sends nothing yet — local
disconnect only — so ship it for ops symmetry, not for Laura's sake).

### Suggested Cedric-side shapes (your side, adapt freely)
- `Workspace.laura?: { orgId, apiToken?, webhookSecret, webhookToken, avatarId?, defaultChannel? }`
- `laura_org_links (org_id TEXT PRIMARY KEY, team_id TEXT NOT NULL)` — in BOTH
  `db/schema.sql` and `ensureSchema()`.
- Webhook auth: read claimed `org_id` from the payload → load that workspace's
  `laura.webhookSecret/webhookToken` → run the EXISTING verify logic
  parameterized `(secret, …)` with the env value as fallback (bit-identical
  when no per-org secret exists — Cedric stays "client #1" on env).
- Event routing fallback: when `external_ref` lacks `team`+`slack_channel`,
  resolve the workspace via `laura_org_links[org_id]` + `laura.defaultChannel`
  instead of dropping the event.

## Hard rules (from the recon)
- Branch off `staging`, PRs as drafts, NEVER touch Cedric `main` or run `vercel --prod`.
- `lib/laura.ts` is the hottest file (Ben in-flight) — keep the auth refactor a separate
  minimal commit; coordinate before merging.
- Schema changes = both `db/schema.sql` AND `ensureSchema()`; Ben runs `/api/admin/init-db`.
- Never copy the legacy `/api/meet/*` fail-open auth; don't target that surface.
- Ben's calls: per-org secrets in Vercel env, `LAURA_DEBUG` removal (dumps raw payloads),
  mid-meeting-execution product call, legacy-path retirement, prod deploys.
