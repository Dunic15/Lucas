"""Browser identities — per-org saved browser logins (Browserbase Contexts).

Revision ID: 0014_browser_identities
Revises: 0013_browser_sessions
Create Date: 2026-07-20

A browser identity binds an org to a provider-side persistent browser
profile (Browserbase Context): the user logs into a site ONCE through the
read-only-token-gated live view, the cookie jar persists encrypted at the
provider, and later operator sessions attach the context and wake up
authenticated. Credentials never transit Laura: the user types them directly
into the provider's live view, and the observation layer already never reads
the cookie jar.

``context_ref`` is the provider's context id — like ``provider_ref`` on
sessions it is NEVER returned by any API or logged; the row's uuid ``id`` is
the only public identifier.

Grant posture matches 0013: SELECT/INSERT/UPDATE to laura_app (no DELETE —
identities are revoked via status, never deleted by the runtime). Downgrade
raises.
"""
from __future__ import annotations

from alembic import op

revision = "0014_browser_identities"
down_revision = "0013_browser_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE public.browser_identities (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          label text NOT NULL,
          provider text NOT NULL DEFAULT 'browserbase',
          context_ref text NOT NULL DEFAULT '',
          status text NOT NULL DEFAULT 'active' CHECK (
            status IN ('active', 'revoked')
          ),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id)
        );
        -- One ACTIVE identity per label; revoked rows stay as audit history.
        CREATE UNIQUE INDEX idx_browser_identities_label
          ON public.browser_identities(org_id, label)
          WHERE status = 'active';
        """
    )

    op.execute(
        """
        ALTER TABLE public.browser_identities ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.browser_identities FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON public.browser_identities
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
        REVOKE ALL ON TABLE public.browser_identities FROM PUBLIC;

        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE public.browser_identities FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE public.browser_identities FROM authenticated;
          END IF;
        END
        $roles$;

        REVOKE ALL ON TABLE public.browser_identities FROM laura_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE
          public.browser_identities TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0014 stores org browser-login bindings; write a reviewed forward "
        "migration instead of dropping identity records."
    )
