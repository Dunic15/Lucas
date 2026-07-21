# AWS self-serve rollout: SSM + App Runner (two-DSN model)

**Status:** staged, not yet applied. Blocked on AWS SSO (see §2).
**Owner of this doc:** infra/AWS lane (Claude, this session). **Do not** touch branches
`claude/selfserve-a-identity` (PR A #154), PR B, PR D; other sessions own them. The
privilege-boundary blocker is already tracked by Codex on PR #154; this doc is the
**infra contract** that satisfies it at the deploy layer, plus the one code change PR A
must make (§6).

Concrete environment (verified 2026-07-13):

| Thing | Value |
|---|---|
| Account | `836739852304` |
| Region | `eu-central-1` |
| App Runner service | `laura-backend`: `arn:aws:apprunner:eu-central-1:836739852304:service/laura-backend/f169c4a486cd47bfac9736ab01367a26` |
| Service URL | `dhfgfe6yw6.eu-central-1.awsapprunner.com` |
| Instance role | `arn:aws:iam::836739852304:role/LauraAppRunnerInstanceRole` |
| Supabase project ref | `vrpevgbmuobduqsmpzgr` (region eu-west-2) |
| Session pooler host | `aws-1-eu-west-2.pooler.supabase.com:5432` |
| Runtime DB role | `laura_app` (non-superuser, non-owner, `rolbypassrls=false`) |

---

## 1. The two-DSN contract (the whole point)

Two Postgres connection strings, two different roles, two different reach:

| Secret | Role | Used by | Injected into App Runner? |
|---|---|---|---|
| `LAURA_DATABASE_URL` | `laura_app` (least-priv, RLS-bound, session pooler) | the **running** service | **YES**: as an SSM secret |
| `LAURA_DATABASE_ADMIN_URL` | owner role (DDL/migrations) | the **one-off Alembic migration job** only | **NO; never reaches the service** |

Rationale: the running app must not hold owner/superuser rights; migrations need DDL.
One URL cannot be both. App Runner therefore gets **only** `LAURA_DATABASE_URL`; the
admin URL lives in SSM and is read **only** by the migration step, out-of-band.

> Verification target on the real Supabase project (blocker items 5–6, owned by PR A's
> test suite, runs on real PG in CI): connected as the runtime role,
> `current_user = 'laura_app'`, `rolsuper = false`, `rolbypassrls = false`.

---

## 2. Prerequisites (must happen before any apply)

1. **Rotate the exposed AWS root key.** The current AWS MCP in the working session
   authenticates as `arn:aws:iam::836739852304:root` and that key was exposed in chat.
   Deactivate + delete it in IAM, review CloudTrail.
2. **Set up scoped access:** `aws configure sso` → a **non-root** role/user with only the
   permissions this runbook needs (`ssm:PutParameter`, `ssm:GetParameter`,
   `apprunner:UpdateService`, `apprunner:DescribeService`, `iam:PutRolePolicy` on the
   instance role).
3. **Reconnect the AWS MCP** to the working session on the scoped creds. Confirm with
   `aws sts get-caller-identity` that it is **not** root.

No SSM/App Runner writes until all three are done.

---

## 3. SSM parameters (target state)

All `SecureString` except where noted. Create/overwrite under `/laura/prod/`.

| Parameter | Type | Notes |
|---|---|---|
| `/laura/prod/LAURA_DATABASE_URL` | SecureString | `postgresql://laura_app.vrpevgbmuobduqsmpzgr:<pw>@aws-1-eu-west-2.pooler.supabase.com:5432/postgres` (already exists; overwrite via file, §5) |
| `/laura/prod/LAURA_DATABASE_ADMIN_URL` | SecureString | owner-role URI, migrations only. **New.** |
| `/laura/prod/STRIPE_SECRET_KEY` | SecureString | rotate the exposed test key first |
| `/laura/prod/STRIPE_WEBHOOK_SECRET` | SecureString | `whsec_…` from Stripe, added after PR C deploy |
| `/laura/prod/SESSION_SECRET` | SecureString | exists |
| `/laura/prod/GOOGLE_CALENDAR_CLIENT_SECRET` | SecureString | ✅ authoritative name (Codex verified code 2026-07-13). Also: `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_REDIRECT_URI`, `PUBLIC_BASE_URL`. Do **not** set a global `GOOGLE_REFRESH_TOKEN`: PR D stores refresh tokens per-org. |

Never inject a secret by literal value on the command line; use `--value file://…`
against a file in the MCP workdir, then delete it (§5).

---

## 4. App Runner environment (target state)

App Runner receives **only** these. `LAURA_DATABASE_ADMIN_URL` is **deliberately absent.**

**RuntimeEnvironmentSecrets** (SSM ARNs):
```
LAURA_DATABASE_URL     -> arn:.../parameter/laura/prod/LAURA_DATABASE_URL
STRIPE_SECRET_KEY      -> arn:.../parameter/laura/prod/STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET  -> arn:.../parameter/laura/prod/STRIPE_WEBHOOK_SECRET
SESSION_SECRET         -> arn:.../parameter/laura/prod/SESSION_SECRET
GOOGLE_CALENDAR_CLIENT_SECRET -> arn:.../parameter/laura/prod/GOOGLE_CALENDAR_CLIENT_SECRET
```

**RuntimeEnvironmentVariables** (plain):
```
STRIPE_PRICE_SOLO             = price_1TsenGAMeOaSiu1OW8xqJzcw
INTERNAL_AVATAR_IDS           = duccio
DASHBOARD_ALLOWED_EMAILS      =        # empty; self-serve gate is per-org, not an allowlist
GOOGLE_CALENDAR_CLIENT_ID     = <from OAuth client>
GOOGLE_CALENDAR_REDIRECT_URI  = https://<PUBLIC_BASE_URL>/oauth/google/callback
PUBLIC_BASE_URL               = https://dhfgfe6yw6.eu-central-1.awsapprunner.com  # → app.lauravatar.com at go-live
```

> Existing env (brain/Recall/Anam/ElevenLabs/Litestream/etc.) stays as-is; this is an
> **additive** change plus the two-var/empty-allowlist edits above. `LAURA_STORE_PATH`
> (SQLite) can remain as the key-free fallback; the durable control plane activates iff
> `LAURA_DATABASE_URL` is set (PR A behavior).

Applying env changes triggers an App Runner deploy → run the **deploy guard** (§7).

---

## 5. Store `LAURA_DATABASE_URL` without exposing the secret

1. Human retrieves/builds the `laura_app` session-pooler URI (owner: Duccio, Supabase).
2. Writes it to the MCP workdir file (never chat/history):
   ```bash
   pbpaste > "<mcp-workdir>/db_uri.txt"
   ```
3. Claude (scoped creds):
   ```
   aws ssm put-parameter --name /laura/prod/LAURA_DATABASE_URL \
     --type SecureString --value file://db_uri.txt --overwrite --region eu-central-1
   ```
4. Verify version bumped **without** decrypting:
   ```
   aws ssm describe-parameters --parameter-filters \
     Key=Name,Option=Equals,Values=/laura/prod/LAURA_DATABASE_URL \
     --query "Parameters[].{Name:Name,Version:Version,Modified:LastModifiedDate}"
   ```
5. Human deletes the file. Same flow for `LAURA_DATABASE_ADMIN_URL`, Google/Stripe secrets.

---

## 6. Migration job (where ADMIN_URL is used): **code change owned by PR A**

Today PR A's `backend/alembic/env.py` reads `settings.laura_database_url` for
`alembic upgrade head`. Under the two-DSN model the migration step must prefer the admin
URL:

```python
# env.py (PR A to implement: not edited from this session)
DATABASE_URL = (settings.laura_database_admin_url or settings.laura_database_url).strip()
```

Migrations run as a **separate step** (CI job / one-shot task) that has
`LAURA_DATABASE_ADMIN_URL` in its environment. The App Runner service never does. This
doc records the contract; the code lands on branch `claude/selfserve-a-identity`.

---

## 7. Apply sequence (post-SSO, ordered)

1. Prereqs §2 done; MCP is non-root.
2. `put-parameter` the new/updated SSM secrets (§3, §5).
3. Least-priv instance-role policy in place (§8).
4. **Deploy guard (separate step, do not batch):**
   - `GET https://dhfgfe6yw6.eu-central-1.awsapprunner.com/health` → `active_sessions == 0`
   - `aws apprunner list-operations …` → no `IN_PROGRESS` op; service `RUNNING`
   - `gh pr list`: confirm no other session is mid-merge to `main`
5. `aws apprunner update-service …` with the §4 env → wait for `RUNNING`.
6. Run the migration job with `LAURA_DATABASE_ADMIN_URL` (§6).
7. Verify (§9).

Merge-train order (owned across sessions): fix+merge PR A #154 → PR B → PR C → PR D →
rebase + merge #153 → this infra apply → public deploy.

---

## 8. Least-privilege instance role

Attach to `LauraAppRunnerInstanceRole`: read only the Laura params it needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LauraSsmRead",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:GetParameters"],
      "Resource": "arn:aws:ssm:eu-central-1:836739852304:parameter/laura/prod/*"
    },
    {
      "Sid": "LauraKmsDecrypt",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "*",
      "Condition": { "StringEquals": { "kms:ViaService": "ssm.eu-central-1.amazonaws.com" } }
    }
  ]
}
```
Drop the KMS statement if using the AWS-managed `alias/aws/ssm` default key. Do **not**
grant the instance role access to `LAURA_DATABASE_ADMIN_URL` if it is stored outside
`/laura/prod/*`: better: keep the admin URL under a path the instance role's ARN pattern
does **not** match (e.g. `/laura/admin/…`) so the service physically cannot read it.

> Refinement: put the admin URL at `/laura/admin/LAURA_DATABASE_ADMIN_URL`, scope the
> instance role to `/laura/prod/*` only, and give the migration job a separate role
> scoped to `/laura/admin/*`. Then "never available to App Runner" is enforced by IAM,
> not just by omission from the env map.

---

## 9. Verification (done = all green)

- App Runner env contains `LAURA_DATABASE_URL`, **not** `LAURA_DATABASE_ADMIN_URL`
  (`describe-service` → grep the env/secrets map).
- On real Supabase as the runtime role: `current_user='laura_app'`, `rolsuper=false`,
  `rolbypassrls=false` (PR A's real-PG test).
- Two-org isolation test passes against real Supabase (PR A).
- `/health` returns `200` and the durable control plane is active (Postgres path, not
  SQLite fallback).
- Supabase advisor: `users`/`alembic_version` + missing-FK-index findings resolved or
  explicitly documented (blocker item 7; owned by PR A).

---

## Open items / owners

| Item | Owner |
|---|---|
| PR A privilege-boundary code (env.py admin-URL split, private-schema / RLS finalize) | session on `claude/selfserve-a-identity` |
| Rotate root key + AWS SSO + reconnect MCP | Duccio |
| Rotate Stripe test key + Supabase secret key | Duccio |
| Apply SSM + App Runner + least-priv role | Claude (this lane, post-SSO) |
| `GOOGLE_CLIENT_SECRET` vs `GOOGLE_CALENDAR_CLIENT_SECRET` name reconciliation | needs one owner (code + SSM) |
| Stripe webhook secret into SSM (post PR C deploy) | Duccio → Claude stores |
