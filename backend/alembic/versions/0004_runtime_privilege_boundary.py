"""Least-privileged runtime boundary for the durable control plane.

Revision ID: 0004_runtime_privilege_boundary
Revises: 0003_usage
Create Date: 2026-07-13

Alembic runs this as the migration owner via LAURA_DATABASE_ADMIN_URL. The
application itself connects only as laura_app (NOSUPERUSER, NOBYPASSRLS).
The exact identity, token resolution, restart inventory, and Stripe event
idempotency operations are exposed through narrowly scoped SECURITY DEFINER functions in a non-exposed schema; all normal tenant
CRUD keeps using FORCE RLS and transaction-local app.current_org.
"""
from __future__ import annotations

from alembic import op

revision = "0004_runtime_privilege_boundary"
down_revision = "0003_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        DO $preflight$
        DECLARE
          app_super boolean;
          app_bypass boolean;
          app_login boolean;
          owner_privileged boolean;
        BEGIN
          SELECT r.rolsuper, r.rolbypassrls, r.rolcanlogin
            INTO app_super, app_bypass, app_login
            FROM pg_catalog.pg_roles AS r
           WHERE r.rolname = 'laura_app';
          IF NOT FOUND THEN
            RAISE EXCEPTION 'required runtime role laura_app does not exist';
          END IF;
          IF app_super OR app_bypass OR NOT app_login THEN
            RAISE EXCEPTION
              'laura_app must be LOGIN NOSUPERUSER NOBYPASSRLS';
          END IF;
          SELECT r.rolsuper OR r.rolbypassrls
            INTO owner_privileged
            FROM pg_catalog.pg_roles AS r
           WHERE r.rolname = current_user;
          IF NOT COALESCE(owner_privileged, false) THEN
            RAISE EXCEPTION
              'migration/definer owner must be SUPERUSER or BYPASSRLS';
          END IF;
        END
        $preflight$;

        CREATE SCHEMA IF NOT EXISTS laura_private;
        REVOKE ALL ON SCHEMA laura_private FROM PUBLIC;
        GRANT USAGE ON SCHEMA laura_private TO laura_app;

        CREATE OR REPLACE FUNCTION laura_private.ensure_user(
          p_google_sub text,
          p_email text,
          p_name text DEFAULT ''
        )
        RETURNS TABLE (
          user_id uuid,
          org_id uuid,
          normalized_email text,
          created boolean
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
        DECLARE
          v_sub text := pg_catalog.btrim(COALESCE(p_google_sub, ''));
          v_email text := pg_catalog.lower(
            pg_catalog.btrim(COALESCE(p_email, ''))
          );
          v_name text := COALESCE(p_name, '');
          v_user_id uuid;
          v_inserted_user_id uuid;
          v_org_id uuid;
          v_created boolean := false;
          v_domain text;
          v_local text;
          v_membership_status text;
          v_membership_exists boolean := false;
        BEGIN
          IF v_email = '' THEN
            RETURN;
          END IF;

          -- Serialize both unique identities so concurrent OAuth callbacks
          -- converge without exposing the users table to the runtime role.
          PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('laura:email:' || v_email, 0)
          );
          IF v_sub <> '' THEN
            PERFORM pg_catalog.pg_advisory_xact_lock(
              pg_catalog.hashtextextended('laura:sub:' || v_sub, 0)
            );
          END IF;

          IF v_sub <> '' THEN
            SELECT u.id INTO v_user_id
              FROM public.users AS u
             WHERE u.google_sub = v_sub
             LIMIT 1;
          END IF;
          IF v_user_id IS NULL THEN
            SELECT u.id INTO v_user_id
              FROM public.users AS u
             WHERE u.email = v_email
             LIMIT 1;
          END IF;

          IF v_user_id IS NULL THEN
            v_user_id := pg_catalog.gen_random_uuid();
            INSERT INTO public.users AS u (
              id, email, name, google_sub, provider, auth_method
            )
            VALUES (
              v_user_id, v_email, v_name, NULLIF(v_sub, ''),
              'google', 'google'
            )
            ON CONFLICT DO NOTHING
            RETURNING u.id INTO v_inserted_user_id;

            v_created := v_inserted_user_id IS NOT NULL;
            IF v_inserted_user_id IS NULL THEN
              v_user_id := NULL;
              IF v_sub <> '' THEN
                SELECT u.id INTO v_user_id
                  FROM public.users AS u
                 WHERE u.google_sub = v_sub
                 LIMIT 1;
              END IF;
              IF v_user_id IS NULL THEN
                SELECT u.id INTO v_user_id
                  FROM public.users AS u
                 WHERE u.email = v_email
                 LIMIT 1;
              END IF;
            END IF;
          ELSE
            UPDATE public.users AS u
               SET name = CASE WHEN v_name <> '' THEN v_name ELSE u.name END,
                   email = v_email,
                   google_sub = CASE
                     WHEN (u.google_sub IS NULL OR u.google_sub = '')
                       THEN NULLIF(v_sub, '')
                     ELSE u.google_sub
                   END
             WHERE u.id = v_user_id;
          END IF;

          IF v_user_id IS NULL THEN
            RAISE EXCEPTION 'identity provisioning failed';
          END IF;

          -- A verified corporate domain wins; consumer/unverified domains do
          -- not map. The function returns only the chosen org UUID.
          v_domain := pg_catalog.split_part(v_email, '@', 2);
          IF v_domain <> '' THEN
            SELECT d.org_id INTO v_org_id
              FROM public.org_domains AS d
             WHERE pg_catalog.lower(d.domain) = v_domain
               AND d.verified_at IS NOT NULL
             ORDER BY d.verified_at
             LIMIT 1;
          END IF;

          IF v_org_id IS NOT NULL THEN
            SELECT m.status INTO v_membership_status
              FROM public.memberships AS m
             WHERE m.user_id = v_user_id
               AND m.org_id = v_org_id;
            v_membership_exists := FOUND;
            IF v_membership_exists AND v_membership_status <> 'active' THEN
              RAISE EXCEPTION 'corporate membership is not active'
                USING ERRCODE = '42501';
            END IF;
            IF NOT v_membership_exists THEN
              INSERT INTO public.memberships (user_id, org_id, role, status)
              VALUES (v_user_id, v_org_id, 'member', 'active');
            END IF;
          ELSE
            SELECT m.org_id INTO v_org_id
              FROM public.memberships AS m
              JOIN public.orgs AS o
                ON o.id = m.org_id
               AND o.deleted_at IS NULL
             WHERE m.user_id = v_user_id
               AND m.role = 'owner'
             ORDER BY o.created_at
             LIMIT 1;

            IF v_org_id IS NULL THEN
              v_org_id := pg_catalog.gen_random_uuid();
              v_local := pg_catalog.split_part(v_email, '@', 1);
              IF v_local = '' THEN
                v_local := 'personal';
              END IF;

              INSERT INTO public.orgs (id, name, slug, plan)
              VALUES (
                v_org_id,
                pg_catalog.left(v_local, 80),
                'org-' || pg_catalog.left(
                  pg_catalog.replace(v_org_id::text, '-', ''), 12
                ),
                'free'
              );
              INSERT INTO public.memberships (user_id, org_id, role, status)
              VALUES (v_user_id, v_org_id, 'owner', 'active');
              INSERT INTO public.org_agents (org_id, avatar_id)
              VALUES (v_org_id, 'laura'), (v_org_id, 'cedric');
              INSERT INTO public.billing_accounts (
                org_id, plan, included_seconds
              )
              VALUES (v_org_id, 'free', 900);
            END IF;
          END IF;

          RETURN QUERY SELECT v_user_id, v_org_id, v_email, v_created;
        END
        $function$;

        CREATE OR REPLACE FUNCTION laura_private.resolve_org_token(
          p_token_hash text
        )
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
          SELECT t.org_id
            FROM public.org_tokens AS t
           WHERE t.token_hash = p_token_hash
           LIMIT 1
        $function$;

        CREATE UNIQUE INDEX IF NOT EXISTS uq_org_domains_verified_lower
          ON public.org_domains ((pg_catalog.lower(domain)))
          WHERE verified_at IS NOT NULL;

        CREATE OR REPLACE FUNCTION laura_private.claim_stripe_event(
          p_event_id text,
          p_event_type text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
        DECLARE
          inserted_id text;
        BEGIN
          IF pg_catalog.btrim(COALESCE(p_event_id, '')) = '' THEN
            RETURN false;
          END IF;
          INSERT INTO public.stripe_events (event_id, type)
          VALUES (
            pg_catalog.btrim(p_event_id),
            pg_catalog.left(COALESCE(p_event_type, ''), 120)
          )
          ON CONFLICT (event_id) DO NOTHING
          RETURNING event_id INTO inserted_id;
          RETURN inserted_id IS NOT NULL;
        END
        $function$;

        CREATE OR REPLACE FUNCTION laura_private.list_open_usage_sessions()
        RETURNS TABLE (
          org_id uuid,
          bot_id text,
          avatar_id text,
          state text,
          created_at_epoch double precision,
          in_call_at_epoch double precision,
          deadline_epoch double precision
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
          SELECT u.org_id, u.bot_id, u.avatar_id, u.state,
                 EXTRACT(EPOCH FROM u.created_at),
                 EXTRACT(EPOCH FROM u.in_call_at),
                 EXTRACT(EPOCH FROM u.deadline)
            FROM public.usage_sessions AS u
           WHERE u.state IN ('pending', 'active')
        $function$;

        REVOKE ALL ON FUNCTION
          laura_private.ensure_user(text, text, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION
          laura_private.resolve_org_token(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION
          laura_private.list_open_usage_sessions() FROM PUBLIC;
        REVOKE ALL ON FUNCTION
          laura_private.claim_stripe_event(text, text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION
          laura_private.ensure_user(text, text, text) TO laura_app;
        GRANT EXECUTE ON FUNCTION
          laura_private.resolve_org_token(text) TO laura_app;
        GRANT EXECUTE ON FUNCTION
          laura_private.list_open_usage_sessions() TO laura_app;
        GRANT EXECUTE ON FUNCTION
          laura_private.claim_stripe_event(text, text) TO laura_app;

        -- Supabase normally has these roles; embedded/plain Postgres may not.
        -- PUBLIC is already revoked above. These explicit revokes also remove
        -- any direct grant if a role pre-existed the migration.
        DO $roles$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON SCHEMA laura_private FROM anon;
            REVOKE ALL ON FUNCTION
              laura_private.ensure_user(text, text, text) FROM anon;
            REVOKE ALL ON FUNCTION
              laura_private.resolve_org_token(text) FROM anon;
            REVOKE ALL ON FUNCTION
              laura_private.list_open_usage_sessions() FROM anon;
            REVOKE ALL ON FUNCTION
              laura_private.claim_stripe_event(text, text) FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON SCHEMA laura_private FROM authenticated;
            REVOKE ALL ON FUNCTION
              laura_private.ensure_user(text, text, text) FROM authenticated;
            REVOKE ALL ON FUNCTION
              laura_private.resolve_org_token(text) FROM authenticated;
            REVOKE ALL ON FUNCTION
              laura_private.list_open_usage_sessions() FROM authenticated;
            REVOKE ALL ON FUNCTION
              laura_private.claim_stripe_event(text, text) FROM authenticated;
          END IF;
        END
        $roles$;

        -- Reset pre-existing/default grants before applying the exact runtime
        -- allowlist. This migration is safe even if an operator accidentally
        -- granted these objects broadly before the fix-forward.
        REVOKE ALL ON TABLE
          public.users, public.orgs, public.memberships, public.org_domains,
          public.org_agents, public.org_tokens, public.org_connections,
          public.billing_accounts, public.usage_sessions, public.stripe_events,
          public.audit_log
          FROM PUBLIC;
        REVOKE ALL ON TABLE
          public.users, public.orgs, public.memberships, public.org_domains,
          public.org_agents, public.org_tokens, public.org_connections,
          public.billing_accounts, public.usage_sessions, public.stripe_events,
          public.audit_log
          FROM laura_app;

        DO $table_roles$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE
              public.users, public.orgs, public.memberships, public.org_domains,
              public.org_agents, public.org_tokens, public.org_connections,
              public.billing_accounts, public.usage_sessions,
              public.stripe_events, public.audit_log
              FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE
              public.users, public.orgs, public.memberships, public.org_domains,
              public.org_agents, public.org_tokens, public.org_connections,
              public.billing_accounts, public.usage_sessions,
              public.stripe_events, public.audit_log
              FROM authenticated;
          END IF;
        END
        $table_roles$;

        GRANT USAGE ON SCHEMA public TO laura_app;
        GRANT INSERT, DELETE ON TABLE public.org_tokens TO laura_app;
        GRANT SELECT (org_id, label) ON TABLE public.org_tokens TO laura_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE
          public.org_connections, public.billing_accounts,
          public.usage_sessions TO laura_app;
        """
    )

    # SET LOCAL leaves an empty-string placeholder on some pooled connections.
    # NULLIF prevents a reused no-context connection from raising invalid_uuid
    # while retaining default-deny semantics.
    rls_tables = (
        ("sessions", "org_id"),
        ("utterances", "org_id"),
        ("conversation_routes", "org_id"),
        ("artifacts", "org_id"),
        ("scheduled_events", "org_id"),
        ("ledger_items", "org_id"),
        ("orgs", "id"),
        ("memberships", "org_id"),
        ("org_domains", "org_id"),
        ("org_agents", "org_id"),
        ("org_tokens", "org_id"),
        ("audit_log", "org_id"),
        ("org_connections", "org_id"),
        ("billing_accounts", "org_id"),
        ("usage_sessions", "org_id"),
    )
    for table, column in rls_tables:
        op.execute(
            f"""
            DROP POLICY IF EXISTS tenant_isolation ON public.{table};
            CREATE POLICY tenant_isolation ON public.{table}
              USING (
                {column} = NULLIF(
                  current_setting('app.current_org', true), ''
                )::uuid
              )
              WITH CHECK (
                {column} = NULLIF(
                  current_setting('app.current_org', true), ''
                )::uuid
              );
            """
        )


def downgrade() -> None:
    # Removing this boundary would either strand runtime signups or invite the
    # owner credential back into the service. Roll forward with a reviewed
    # replacement instead.
    raise NotImplementedError(
        "0004 is the production privilege boundary; replace it with a "
        "forward migration instead of restoring an owner-powered runtime."
    )
