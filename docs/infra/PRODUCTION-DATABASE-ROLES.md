# Production database roles

Laura uses two database credentials with intentionally disjoint lifetimes.

| Setting | Database role | Where it exists | Purpose |
|---|---|---|---|
| `LAURA_DATABASE_URL` | `laura_app` with `LOGIN NOSUPERUSER NOBYPASSRLS` | App Runner runtime and CI runtime-role tests | Short control-plane transactions under FORCE RLS |
| `LAURA_DATABASE_ADMIN_URL` | migration owner / DDL role | one-off migration job or operator shell only | `alembic upgrade head`, schema/function ownership and grants |

## Non-negotiable deployment rule

**Never add `LAURA_DATABASE_ADMIN_URL` to App Runner.** The service secret
allowlist contains the runtime URL only. If the owner URL is present in an App
Runner revision, remove it and rotate that credential before serving traffic.

Recommended SSM names:

- `/laura/prod/LAURA_DATABASE_URL` — the `laura_app` pooler DSN.
- `/laura/prod/LAURA_DATABASE_ADMIN_URL` — the owner DSN, readable only by
  the migration operator/job role.

The App Runner instance role gets `ssm:GetParameter` (and `kms:Decrypt` when
using a customer KMS key) only for the exact runtime parameters. It must not
have permission to read the admin parameter.

## Migration runbook

1. Open a one-off shell/job whose IAM principal can read only the admin
   parameter required for migration.
2. Export `LAURA_DATABASE_ADMIN_URL` and
   `LAURA_REQUIRE_MIGRATIONS=1` there. Do not export
   `LAURA_DATABASE_URL` as a substitute. The require flag makes a
   missing/empty admin secret a hard failure instead of the local-demo no-op.
3. Run `cd backend && python -m alembic upgrade head`.
4. Remove the admin secret from the job environment when it exits.
5. Deploy App Runner with `LAURA_DATABASE_URL` mapped to the `laura_app`
   credential and with no admin mapping.

Alembic intentionally exits without touching a database when
`LAURA_DATABASE_ADMIN_URL` is empty. The runtime URL is ignored by
`backend/alembic/env.py`, preventing accidental DDL with the service
credential.

## Runtime proof before production traffic

Connect through the same `LAURA_DATABASE_URL` injected into App Runner and
verify:

```sql
select current_user, rolsuper, rolbypassrls
from pg_roles
where rolname = current_user;
```

The only acceptable result is `laura_app | false | false`. Then run the
two-org integration test. It must prove signup succeeds through
`laura_private.ensure_user`, direct identity-table enumeration is denied, and
org A cannot read or write org B rows.

Migration `0004_runtime_privilege_boundary` first checks that `laura_app`
exists with LOGIN, NOSUPERUSER and NOBYPASSRLS, and that the migration/function
owner can bypass FORCE RLS. It also resets any pre-existing grants before
applying the exact allowlist. The private schema owns the org-less operations:
Google sub/email/domain provisioning, token-hash-to-org resolution, global
restart enumeration of open usage, and Stripe event idempotency. They are
SECURITY DEFINER functions in the non-exposed `laura_private` schema with an
empty search path, fully qualified objects, PUBLIC/anon/authenticated revoked,
and exact EXECUTE granted only to `laura_app`. Normal tenant CRUD remains direct and RLS-scoped with transaction-local
`app.current_org`.

Before enabling payments, PR C must claim webhook event ids only through
`laura_private.claim_stripe_event(event_id, event_type)`; the runtime role has
no direct privilege on `stripe_events`. Its integration suite must run through
the `laura_app` DSN and prove signed, idempotent webhook handling before the
payment deployment is approved.

## Rotation note

Do not reuse credentials that have appeared in chat, shell history, logs, or
screenshots. Rotate exposed AWS/database credentials before production and
store only their replacements in SSM SecureString.
