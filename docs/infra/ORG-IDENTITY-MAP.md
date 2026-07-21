# Org identity: the canonical map (read this before diagnosing any org/billing/connect issue)

*Last reviewed: 2026-07-15. Grounded in prod data (Postgres control plane + Litestream replica), not code inference.*

## The one rule

**With the durable control plane enabled (`LAURA_DATABASE_URL` set; true in prod), the identity
source of truth is Postgres `laura_private.ensure_user`, NOT the SQLite seeds.** `store.upsert_user`
calls it on every login and its uuid org **overrides** whatever `org_id_for_email` computed from
SQLite; a control-plane failure rejects the login (fail closed). Any diagnosis that starts from the
SQLite `org_domains` seed alone will be wrong in prod.

## The identities (what each one is: and is not)

| Identity | What it actually is |
|---|---|
| `u_<hash>` (e.g. `u_e318fde84ab471f3`) | `"u_" + sha256(email)[:16]`. In prod: the **user_id / cookie cache key only**: never an org. It is an org only in key-free/demo deployments (control plane off). |
| **uuid orgs** (e.g. `bf4a683b-…` = SFF Studio) | The real orgs. Minted/resolved by Postgres `ensure_user`: verified `org_domains` row → shared org; else existing membership; else a fresh personal uuid org. All billing/entitlements/durable artifacts/outbox key on these (every path casts `::uuid`). |
| `org_sff` | **A dead SQLite seed** (historical). No data row references it anywhere (verified on the live replica 2026-07-15). When the control plane is enabled the seed is skipped and any previously seeded rows are swept at boot. In key-free demo deployments it still exists so the multi-org tests/demos work. |
| `demo_org_id` (`…00de`) | A uuid; the org stamped on key-free/global-bearer sessions AND (today) on calendar/gmail-invited meetings; see the open P1 below. |

## SFF Studio specifically

- Canonical org: `bf4a683b-11b0-4349-8201-45d1d4573912` (Postgres: verified domains **sffstudio.com
  + sff.vc**, memberships duccio + ananth, billing topped up, cedric-brain connected
  `team_id=T0AT2QWB4C8`).
- Cedric workspace `T0AT2QWB4C8` → linked to `bf4a683b` (re-linked 2026-07-15 15:39 UTC via the full
  Add-to-Slack flow; fresh per-org secret in SSM).
- `sffstudio.com`/`sff.vc` logins land on it automatically (ensure_user, verified domains).

## Rules that keep this true

1. **Never re-point the SQLite seed at a live tenant uuid**: repo code stays synthetic.
2. **`entitlements.open_usage` has NO `is_durable_org` guard: by design (fail-closed metering).**
   Don't "fix" 503s by bypassing it; the invariant is that no non-uuid org can reach a dispatch.
3. **A bare `POST /api/laura/orgs` to Cedric is forbidden as a re-link**: it re-mints credentials
   that never reach SSM (webhook-secret drift). The dashboard **Add to Slack** (full `/complete`
   flow) is the only sanctioned re-link.
4. `_restamp_personal_org` moves artifacts/sessions/ledger at cutover login but **not**
   `org_connections`/`org_oauth`/`org_tokens`: a pre-durable user must redo native-Google +
   Add-to-Slack after their first durable login.

## Known open items (tracked separately, do not conflate)

- **P1, calendar/gmail-invited meetings stamp `demo_org_id`** (main.py webhook + gmail-watcher
  default): customer email-invites bill the Demo org and are invisible in the customer dashboard.
  Fix = resolve the org from the inviting calendar/inbox owner.
- **jt@sff.vc** (`u_7c18dc7227fc3e7a`): pre-durable install; must re-login (auto-lands on
  `bf4a683b`), then redo native-Google + Add-to-Slack. Afterwards retire SSM
  `/laura/prod/LAURA_WEBHOOK_SECRETS_BY_ORG/orgs/u_7c18dc7227fc3e7a`.
