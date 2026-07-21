"""Browser operator B0; durable session ownership + presentation tokens.

Revision ID: 0013_browser_sessions
Revises: 0012_data_foundation
Create Date: 2026-07-17

B0 (docs/product/LAURA-SABLE-BROWSER-OPERATOR-IMPLEMENTATION.md §3) proves a
Laura avatar can open and operate a remote browser for a watched demo behind
a read-only live view, with the security boundaries a production operator
needs. Laura owns the canonical session; the provider (fake by default,
Browserbase adapter optional and flag-gated) is replaceable behind the
BrowserOperator interface.

Tables (all FORCE RLS, composite-org FKs, 0009-0012 discipline):

- ``browser_sessions``: the canonical session, tenancy bound to
                                    (org_id, principal, avatar, avatar
                                    version, meeting ref, provider). The
                                    provider's own session id lives in
                                    ``provider_ref`` and is NEVER returned by
                                    any API or logged: Laura's ``id`` (uuid)
                                    is the only public identifier. State
                                    machine + TTL + last command sequence +
                                    the canonical action reference for a
                                    guarded step.
- ``browser_commands``: (org_id, session_id, command_id) idempotency
                                    ledger: a replayed command returns its
                                    first result, never re-executes.
- ``browser_presentation_tokens``: opaque, short-lived, single-session,
                                    read-only viewer grants. Only the sha256
                                    HASH is stored; the token value never
                                    touches the database or logs. Revocable,
                                    expiring, replay-checked.

Guarded WRITE steps need no new action table: they are rows in
``queued_actions`` with ``execution_route='browser'`` (already admitted by
the 0009 CHECK), reusing the M0 decision record + execution claim + receipt.

Grant posture: SELECT/INSERT/UPDATE to laura_app (no DELETE; sessions and
tokens are revoked/expired, never deleted by the runtime; a reviewed
retention worker is future work). Downgrade raises.
"""
from __future__ import annotations

from alembic import op

revision = "0013_browser_sessions"
down_revision = "0012_data_foundation"
branch_labels = None
depends_on = None

_TENANT_TABLES = (
    "browser_sessions",
    "browser_commands",
    "browser_presentation_tokens",
)


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE public.browser_sessions (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          principal text NOT NULL DEFAULT '',
          avatar_key text NOT NULL DEFAULT '',
          avatar_version integer NOT NULL DEFAULT 0,
          meeting_ref text NOT NULL DEFAULT '',
          provider text NOT NULL DEFAULT 'fake',
          provider_ref text NOT NULL DEFAULT '',
          state text NOT NULL DEFAULT 'creating' CHECK (
            state IN ('creating', 'ready', 'presenting', 'closing',
                      'closed', 'failed', 'expired', 'revoked')
          ),
          last_command_seq integer NOT NULL DEFAULT 0,
          page_version integer NOT NULL DEFAULT 0,
          page_fingerprint text NOT NULL DEFAULT '',
          action_ref text NOT NULL DEFAULT '',
          -- Bounded, NON-authoritative demo-run metadata (demo_definition_id/
          -- version, demo_run_id, current_checkpoint). Carried for the demo
          -- integrator; NEVER drives the state machine or provider behaviour.
          metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          expires_at timestamptz NOT NULL,
          PRIMARY KEY (org_id, id)
        );
        CREATE INDEX idx_browser_sessions_live
          ON public.browser_sessions(org_id, expires_at)
          WHERE state IN ('creating', 'ready', 'presenting', 'closing');
        CREATE INDEX idx_browser_sessions_meeting
          ON public.browser_sessions(org_id, meeting_ref)
          WHERE meeting_ref <> '';

        CREATE TABLE public.browser_commands (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          session_id uuid NOT NULL,
          command_id text NOT NULL,
          seq integer NOT NULL,
          verb text NOT NULL,
          result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, session_id, command_id),
          FOREIGN KEY (org_id, session_id)
            REFERENCES public.browser_sessions(org_id, id) ON DELETE CASCADE
        );

        CREATE TABLE public.browser_presentation_tokens (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          session_id uuid NOT NULL,
          token_hash text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          expires_at timestamptz NOT NULL,
          revoked boolean NOT NULL DEFAULT false,
          last_exchanged_at timestamptz,
          exchange_count integer NOT NULL DEFAULT 0,
          PRIMARY KEY (org_id, id),
          UNIQUE (org_id, token_hash),
          FOREIGN KEY (org_id, session_id)
            REFERENCES public.browser_sessions(org_id, id) ON DELETE CASCADE
        );
        CREATE INDEX idx_browser_tokens_session
          ON public.browser_presentation_tokens(org_id, session_id)
          WHERE NOT revoked;
        """
    )

    for table in _TENANT_TABLES:
        op.execute(
            f"""
            ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON public.{table}
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
            """
        )

    op.execute(
        r"""
        -- Cross-org discovery for the expiry reconcile loop: org UUIDs only,
        -- payload reads stay under ordinary RLS (the 0006 due_callback_orgs
        -- pattern).
        CREATE OR REPLACE FUNCTION laura_private.due_browser_orgs(
          p_limit integer DEFAULT 50
        )
        RETURNS SETOF uuid
        LANGUAGE sql
        VOLATILE
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
          SELECT DISTINCT s.org_id
            FROM public.browser_sessions AS s
           WHERE s.state IN ('creating','ready','presenting','closing')
             AND s.expires_at <= pg_catalog.clock_timestamp()
           LIMIT LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200)
        $function$;

        REVOKE ALL ON TABLE
          public.browser_sessions, public.browser_commands,
          public.browser_presentation_tokens
        FROM PUBLIC;
        REVOKE ALL ON FUNCTION
          laura_private.due_browser_orgs(integer) FROM PUBLIC;

        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE
              public.browser_sessions, public.browser_commands,
              public.browser_presentation_tokens FROM anon;
            REVOKE ALL ON FUNCTION
              laura_private.due_browser_orgs(integer) FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE
              public.browser_sessions, public.browser_commands,
              public.browser_presentation_tokens FROM authenticated;
            REVOKE ALL ON FUNCTION
              laura_private.due_browser_orgs(integer) FROM authenticated;
          END IF;
        END
        $roles$;

        REVOKE ALL ON TABLE
          public.browser_sessions, public.browser_commands,
          public.browser_presentation_tokens
        FROM laura_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE
          public.browser_sessions, public.browser_commands,
          public.browser_presentation_tokens TO laura_app;
        GRANT EXECUTE ON FUNCTION
          laura_private.due_browser_orgs(integer) TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0013 is the browser-session boundary; write a reviewed forward "
        "migration instead of dropping session ownership records."
    )
