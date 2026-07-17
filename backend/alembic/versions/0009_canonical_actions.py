"""Canonical Action Control Plane (M0): one action object, every surface.

Revision ID: 0009_canonical_actions
Revises: 0008_personal_orgs_policy
Create Date: 2026-07-17

Extends the durable action spine (0006) so ``queued_actions`` can carry the
full canonical Action object from UNIFIED-ACTION-CONTROL-PLANE.md instead of
just the distilled text triple:

- two new lifecycle states: ``needs_details`` (typed spec is missing required
  parameters — surfaced for editing instead of silently approving a no-op) and
  ``executing`` (an execution claim is held; the atomic ``approved →
  executing`` compare-and-set in outbox_pg.claim_action_execution is what
  makes double-approval single-execution true across App Runner instances);
- safe typed parameters + their JSON parameter schema (``typed_json``,
  ``params_schema_json``) so approval surfaces can render/edit exact fields —
  the SAVED spec stays the only thing that executes, never a client body;
- routing/provenance fields (``risk``, ``execution_route``, ``origin_avatar``,
  ``connected_account_json``, ``permission_json``) persisted durably instead
  of living only inside the artifact blob;
- ``receipt_json`` (structured receipt) and ``logs_json`` (bounded append-only
  step log — distilled one-liners only, never transcript content);
- an enforced idempotency key: UNIQUE(org_id, idempotency_key) WHERE <> '' —
  the same key is passed to Cedric via the existing Idempotency-Key header;
- ``execution_lease_until`` so a crash mid-execution is observable (a stale
  ``executing`` row is reconciled, never blindly retried — the external write
  may have happened);
- a durable ``action_decisions`` table replacing the per-instance SQLite
  ``action_approvals`` convergence when the control plane is on: first write
  wins via the primary key, so two surfaces deciding simultaneously converge
  on ONE recorded decision no matter which instance served each request;
- ``action.updated`` joins the durable callback event vocabulary so parameter
  edits reach Slack cards with outbox-grade delivery.

Everything follows the 0006 discipline: FORCE RLS with the transaction-local
``app.current_org`` policy, REVOKE-then-GRANT to exactly ``laura_app``.
"""
from __future__ import annotations

from alembic import op

revision = "0009_canonical_actions"
down_revision = "0008_personal_orgs_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        -- ── queued_actions: canonical Action fields ────────────────────────
        ALTER TABLE public.queued_actions
          DROP CONSTRAINT IF EXISTS queued_actions_execution_status_check;
        ALTER TABLE public.queued_actions
          ADD CONSTRAINT queued_actions_execution_status_check CHECK (
            execution_status IN (
              '', 'needs_details', 'proposed', 'approved', 'executing',
              'rejected', 'done', 'failed'
            )
          );

        ALTER TABLE public.queued_actions
          ADD COLUMN typed_json jsonb,
          ADD COLUMN params_schema_json jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN risk text NOT NULL DEFAULT ''
            CHECK (risk IN ('', 'low', 'medium', 'high')),
          ADD COLUMN execution_route text NOT NULL DEFAULT ''
            CHECK (execution_route IN ('', 'native', 'cedric', 'browser')),
          ADD COLUMN origin_avatar text NOT NULL DEFAULT '',
          ADD COLUMN connected_account_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN permission_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN receipt_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN logs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN idempotency_key text NOT NULL DEFAULT '',
          ADD COLUMN execution_lease_until timestamptz;

        CREATE UNIQUE INDEX uq_queued_actions_idempotency
          ON public.queued_actions(org_id, idempotency_key)
          WHERE idempotency_key <> '';

        -- ── callback_outbox: action.updated joins the event vocabulary ─────
        ALTER TABLE public.callback_outbox
          DROP CONSTRAINT IF EXISTS callback_outbox_event_check;
        ALTER TABLE public.callback_outbox
          ADD CONSTRAINT callback_outbox_event_check CHECK (
            event IN ('action.requested', 'session.ended', 'action.updated')
          );

        -- ── durable canonical decision record (cross-instance convergence) ─
        CREATE TABLE public.action_decisions (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          action_id text NOT NULL,
          decision text NOT NULL
            CHECK (decision IN ('approve', 'reject', 'respond')),
          selected_slot_id text NOT NULL DEFAULT '',
          idempotency_key text NOT NULL DEFAULT '',
          decided_via text NOT NULL DEFAULT '',
          laura_user_id text NOT NULL DEFAULT '',
          previous_status text NOT NULL DEFAULT '',
          new_status text NOT NULL DEFAULT '',
          execution_job_id text,
          blocked_on text NOT NULL DEFAULT '',
          decided_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, action_id)
        );

        ALTER TABLE public.action_decisions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.action_decisions FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON public.action_decisions
          TO laura_app
          USING (
            org_id = NULLIF(
              current_setting('app.current_org', true), ''
            )::uuid
          )
          WITH CHECK (
            org_id = NULLIF(
              current_setting('app.current_org', true), ''
            )::uuid
          );

        REVOKE ALL ON TABLE public.action_decisions FROM PUBLIC;
        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE public.action_decisions FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE public.action_decisions FROM authenticated;
          END IF;
        END
        $roles$;
        REVOKE ALL ON TABLE public.action_decisions FROM laura_app;
        GRANT SELECT, INSERT, UPDATE
          ON TABLE public.action_decisions TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0009 extends the durable action spine in place; write a reviewed "
        "forward migration instead of dropping canonical action state."
    )
