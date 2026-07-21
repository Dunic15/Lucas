"""usage metering: usage_sessions (the 15-min free entitlement clock) + stripe_events.

Revision ID: 0003_usage
Revises: 0002_control_plane
Create Date: 2026-07-13

PR B of the self-serve flow (docs/product/SELF-SERVE-FLOW-PLAN.md): a durable,
per-org record of avatar-minutes so the free trial survives redeploys and
cannot be double-spent.

- ``usage_sessions``: one row per dispatched bot. Lifecycle:
  ``pending`` (gate passed, bot dispatching) → ``active`` (Recall reported an
  in-call status; ``in_call_at`` is Recall's own timestamp, ``deadline`` =
  in_call_at + the org's remaining seconds at that moment) → ``closed``
  (``consumed_seconds`` final, ``close_reason`` says why). The clock is
  STATUS-based (silent meetings consume); enforcement happens at start
  (entitlements.open_usage) and every reconcile pass; never on the live
  transcript hot path.
- ``uq_usage_one_active_per_org``: a PARTIAL unique index on org_id over
  pending/active rows: ONE concurrent meeting per org, race-safe at the
  database itself (two racing starts can't both insert; the loser sees a
  unique violation, not an overspend).
- ``stripe_events``: processed Stripe webhook event ids (PR C consumes this;
  created here so PR C ships schema-free). Deliberately NO org RLS policy:
  event ids arrive before any org resolution and are handled exclusively by
  the service role (the same owner/BYPASSRLS role the control plane connects
  as); there is no tenant-scoped reader.

``usage_sessions`` gets the SAME RLS pattern as 0001/0002 (ENABLE + FORCE +
tenant_isolation on ``current_setting('app.current_org')``), so a policy-bound
role is isolated by the database, not by remembered WHEREs.

Runnable on plain PG13+: gen_random_uuid() is core since PG13; no new
extension requirements.
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_usage"
down_revision = "0002_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── the per-meeting usage clock ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_sessions (
          id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          org_id           uuid NOT NULL REFERENCES orgs(id),
          bot_id           text UNIQUE NOT NULL,
          avatar_id        text NOT NULL DEFAULT '',
          state            text NOT NULL DEFAULT 'pending',  -- pending|active|closed
          created_at       timestamptz NOT NULL DEFAULT now(),
          in_call_at       timestamptz,
          deadline         timestamptz,
          closed_at        timestamptz,
          consumed_seconds int  NOT NULL DEFAULT 0,
          close_reason     text NOT NULL DEFAULT ''
        );
        """
    )
    # One concurrent meeting per org; enforced by the DATABASE so two racing
    # starts can never both dispatch a paid bot (the whole point of PR B's
    # atomic gate). Closed rows drop out of the index.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_one_active_per_org
            ON usage_sessions (org_id) WHERE state IN ('pending', 'active');
        """
    )
    # Remaining-seconds math scans an org's closed rows every gate/reconcile.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_org_state
            ON usage_sessions (org_id, state);
        """
    )

    # ── RLS: same ENABLE + FORCE + tenant_isolation pattern as 0001/0002 ──
    op.execute(
        """
        ALTER TABLE usage_sessions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE usage_sessions FORCE  ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation ON usage_sessions;
        CREATE POLICY tenant_isolation ON usage_sessions
          USING      (org_id = current_setting('app.current_org', true)::uuid)
          WITH CHECK (org_id = current_setting('app.current_org', true)::uuid);
        """
    )

    # ── Stripe webhook idempotency ledger (PR C consumes) ──
    # NOTE: deliberately NO org RLS policy; this table is GLOBAL and touched
    # only by the service role (webhook events precede org resolution; a
    # tenant-scoped role has no business reading raw Stripe event ids).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS stripe_events (
          event_id     text PRIMARY KEY,
          type         text,
          processed_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    # Mirrors 0001/0002's stance: dropping usage_sessions destroys the trial
    # accounting (a redeploy-reset trial is the exact bug this PR closes).
    raise NotImplementedError(
        "0003 adds the durable usage/entitlement spine; downgrading would "
        "reset every org's consumed trial minutes."
    )
