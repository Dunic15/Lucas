"""Stripe billing state and least-privilege webhook boundary.

Revision ID: 0005_billing_period
Revises: 0004_runtime_privilege_boundary
Create Date: 2026-07-13

The runtime remains laura_app (NOSUPERUSER, NOBYPASSRLS). It never receives
SELECT on the global stripe_events table and cannot enumerate customer mappings.
A narrowly-scoped SECURITY DEFINER function claims an event and resolves/locks
one customer mapping in the caller transaction; a later tenant write and the
claim therefore commit or roll back together.
"""
from __future__ import annotations

from alembic import op

revision = "0005_billing_period"
down_revision = "0004_runtime_privilege_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE public.billing_accounts
          ADD COLUMN IF NOT EXISTS current_period_start timestamptz,
          ADD COLUMN IF NOT EXISTS subscription_event_created bigint NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS subscription_event_id text NOT NULL DEFAULT '',
          ADD COLUMN IF NOT EXISTS invoice_event_created bigint NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS invoice_event_id text NOT NULL DEFAULT '',
          ADD COLUMN IF NOT EXISTS checkout_event_created bigint NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS checkout_event_id text NOT NULL DEFAULT '',
          ADD COLUMN IF NOT EXISTS last_checkout_session_id text,
          ADD COLUMN IF NOT EXISTS last_checkout_subscription_id text,
          ADD COLUMN IF NOT EXISTS checkout_revision bigint NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS checkout_pending_until timestamptz,
          ADD COLUMN IF NOT EXISTS verified_paid_subscription_id text,
          ADD COLUMN IF NOT EXISTS verified_paid_period_start timestamptz,
          ADD COLUMN IF NOT EXISTS verified_paid_period_end timestamptz,
          ADD COLUMN IF NOT EXISTS verified_paid_event_created bigint NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS verified_paid_event_id text NOT NULL DEFAULT '';

        CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_stripe_customer
          ON public.billing_accounts (stripe_customer_id)
          WHERE stripe_customer_id IS NOT NULL;

        CREATE OR REPLACE FUNCTION laura_private.prevent_stripe_customer_rebind()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = ''
        AS $function$
        BEGIN
          IF OLD.stripe_customer_id IS NOT NULL
             AND NEW.stripe_customer_id IS DISTINCT FROM OLD.stripe_customer_id
          THEN
            RAISE EXCEPTION 'stripe customer binding is immutable'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $function$;

        DROP TRIGGER IF EXISTS billing_customer_immutable
          ON public.billing_accounts;
        CREATE TRIGGER billing_customer_immutable
          BEFORE UPDATE OF stripe_customer_id ON public.billing_accounts
          FOR EACH ROW
          EXECUTE FUNCTION laura_private.prevent_stripe_customer_rebind();

        CREATE OR REPLACE FUNCTION laura_private.claim_billing_event(
          p_event_id text,
          p_event_type text,
          p_customer_id text
        )
        RETURNS TABLE (claimed boolean, resolved_org_id uuid)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
        DECLARE
          v_inserted text;
          v_org uuid;
        BEGIN
          IF pg_catalog.btrim(COALESCE(p_event_id, '')) = ''
             OR pg_catalog.length(p_event_id) > 255
          THEN
            RAISE EXCEPTION 'invalid stripe event id'
              USING ERRCODE = '22023';
          END IF;

          INSERT INTO public.stripe_events (event_id, type)
          VALUES (
            pg_catalog.btrim(p_event_id),
            pg_catalog.left(COALESCE(p_event_type, ''), 120)
          )
          ON CONFLICT (event_id) DO NOTHING
          RETURNING event_id INTO v_inserted;

          IF v_inserted IS NULL THEN
            RETURN QUERY SELECT false, NULL::uuid;
            RETURN;
          END IF;

          IF pg_catalog.btrim(COALESCE(p_customer_id, '')) <> '' THEN
            SELECT b.org_id INTO v_org
              FROM public.billing_accounts AS b
             WHERE b.stripe_customer_id = pg_catalog.btrim(p_customer_id)
             FOR UPDATE;
          END IF;

          RETURN QUERY SELECT true, v_org;
        END
        $function$;

        CREATE OR REPLACE FUNCTION laura_private.billing_member_role(
          p_org_id uuid,
          p_user_id uuid
        )
        RETURNS text
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
          SELECT m.role
            FROM public.memberships AS m
           WHERE m.org_id = p_org_id
             AND m.user_id = p_user_id
             AND m.status = 'active'
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION laura_private.billing_boundary_ready()
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
          SELECT true
        $function$;

        REVOKE ALL ON FUNCTION
          laura_private.claim_billing_event(text, text, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION
          laura_private.billing_member_role(uuid, uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION
          laura_private.billing_boundary_ready() FROM PUBLIC;
        REVOKE ALL ON FUNCTION
          laura_private.prevent_stripe_customer_rebind() FROM PUBLIC;

        GRANT EXECUTE ON FUNCTION
          laura_private.claim_billing_event(text, text, text) TO laura_app;
        GRANT EXECUTE ON FUNCTION
          laura_private.billing_member_role(uuid, uuid) TO laura_app;
        GRANT EXECUTE ON FUNCTION
          laura_private.billing_boundary_ready() TO laura_app;

        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON FUNCTION
              laura_private.claim_billing_event(text, text, text) FROM anon;
            REVOKE ALL ON FUNCTION
              laura_private.billing_member_role(uuid, uuid) FROM anon;
            REVOKE ALL ON FUNCTION
              laura_private.billing_boundary_ready() FROM anon;
            REVOKE ALL ON FUNCTION
              laura_private.prevent_stripe_customer_rebind() FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON FUNCTION
              laura_private.claim_billing_event(text, text, text)
              FROM authenticated;
            REVOKE ALL ON FUNCTION
              laura_private.billing_member_role(uuid, uuid)
              FROM authenticated;
            REVOKE ALL ON FUNCTION
              laura_private.billing_boundary_ready() FROM authenticated;
            REVOKE ALL ON FUNCTION
              laura_private.prevent_stripe_customer_rebind()
              FROM authenticated;
          END IF;
        END
        $roles$;

        -- The exact table allowlist remains tenant-scoped by FORCE RLS.
        -- stripe_events deliberately stays absent: only claim_billing_event can
        -- touch that global idempotency table.
        GRANT SELECT, INSERT, UPDATE ON TABLE
          public.billing_accounts TO laura_app;
        REVOKE ALL ON TABLE public.stripe_events FROM laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0005 is the money and privilege boundary; replace it with a reviewed "
        "forward migration instead of weakening subscription state."
    )
