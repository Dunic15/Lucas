"""control plane: google_sub identity + durable org_connections + billing_accounts.

Revision ID: 0002_control_plane
Revises: 0001_org_id_spine
Create Date: 2026-07-13

The self-serve identity/billing additions on top of the 0001 spine
(docs/product/SELF-SERVE-FLOW-PLAN.md):

- ``users.google_sub`` — the durable Google OIDC subject. Nullable (pre-OIDC
  rows, token-only users) with a UNIQUE partial index; ``control_plane.
  ensure_user`` looks a login up by sub first and backfills it by email.
- ``org_connections`` — the durable mirror of the SQLite Configure-tab table
  (same shape: config_json is NON-SECRET wiring only; credentials live in
  env/SSM, never here). PK (org_id, avatar_id, provider).
- ``billing_accounts`` — one row per org: plan + included_seconds (the
  15-minute free trial = 900) + the Stripe linkage PR B fills in.

Both new tables get the SAME RLS pattern as 0001 (ENABLE + FORCE + a
tenant_isolation policy on ``current_setting('app.current_org')``), so a
policy-bound role is isolated by the database itself, not by remembered WHEREs.

Runnable on plain PG13+: ``gen_random_uuid()`` is core since PG13 and 0001
already issues the (redundant there) CREATE EXTENSION statements — this
migration deliberately adds no new extension requirements.
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_control_plane"
down_revision = "0001_org_id_spine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users.google_sub (nullable; unique where present) ──
    op.execute(
        """
        ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sub text;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_google_sub
            ON users (google_sub) WHERE google_sub IS NOT NULL;
        """
    )

    # ── durable Configure-tab mirror (store.py org_connections shape) ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS org_connections (
          org_id      uuid NOT NULL REFERENCES orgs(id),
          avatar_id   text NOT NULL,
          provider    text NOT NULL,   -- cedric-brain | gmail | calendar | slack | drive
          status      text NOT NULL,   -- connected | pending | disconnected
          config_json text NOT NULL DEFAULT '',
          updated_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, avatar_id, provider)
        );
        """
    )

    # ── billing: one account per org (free trial seconds live here) ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_accounts (
          org_id                 uuid PRIMARY KEY REFERENCES orgs(id),
          plan                   text NOT NULL DEFAULT 'free',
          included_seconds       int  NOT NULL DEFAULT 900,
          stripe_customer_id     text,
          stripe_subscription_id text,
          subscription_status    text NOT NULL DEFAULT 'none',
          current_period_end     timestamptz,
          updated_at             timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    # ── RLS: same ENABLE + FORCE + tenant_isolation pattern as 0001 ──
    for table in ("org_connections", "billing_accounts"):
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE  ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS tenant_isolation ON {table};
            CREATE POLICY tenant_isolation ON {table}
              USING      (org_id = current_setting('app.current_org', true)::uuid)
              WITH CHECK (org_id = current_setting('app.current_org', true)::uuid);
            """
        )


def downgrade() -> None:
    # Mirrors 0001's stance: dropping billing_accounts/org_connections destroys
    # tenant billing + connection state (and google_sub severs durable logins).
    # Intentionally not reversible.
    raise NotImplementedError(
        "0002 adds the durable identity/billing spine; downgrading would drop "
        "tenant billing and connection state."
    )
