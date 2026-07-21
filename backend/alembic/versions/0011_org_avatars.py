"""Org-personalized avatar overlays (M2) on the RLS control plane.

Revision ID: 0011_org_avatars
Revises: 0010_company_brain
Create Date: 2026-07-17

The repo's ``avatars/<id>/avatar.yaml`` files stay the immutable canonical
definitions; the capability CEILING an org can never widen. These tables add
the org-owned personalization layer:

- ``org_avatars``: one row per (org, canonical avatar key):
                               enabled switch + the currently published
                               version pointer + actor provenance.
- ``org_avatar_versions``: immutable overlay versions. Drafts are edited
                               in place (optimistic ``version_token``); a
                               publish freezes the row forever and moves the
                               ``org_avatars.current_version`` pointer;
                               rollback = republishing an older version's
                               payload as a NEW version (history is linear
                               and append-only, nothing is ever rewritten).
- ``org_avatar_assignments``: which overlayed avatar applies in which
                               context. Scopes match what the application
                               actually supports today: ``org_default`` and
                               ``user`` (an explicit avatar on the session
                               request always wins; richer scopes arrive with
                               their runtime concepts, never speculatively).
- ``org_avatar_audit``: append-only actor trail for every create /
                               edit / publish / rollback / assignment write.
                               laura_app gets INSERT+SELECT only; the grant
                               IS the append-only guarantee (the PG audit_log
                               from 0001 is deliberately out of laura_app's
                               reach, so this table is the smallest compliant
                               substitute).

The overlay payload is a VALIDATED, allowlisted JSONB (see
``backend/app/avatar_overlay.py``); never raw system-prompt replacement,
never credentials, never capability widening. All tables follow the
0006/0009/0010 discipline: FORCE RLS with the transaction-local
``app.current_org`` NULLIF policy TO laura_app, REVOKE-then-GRANT.
"""
from __future__ import annotations

from alembic import op

revision = "0011_org_avatars"
down_revision = "0010_company_brain"
branch_labels = None
depends_on = None

_TENANT_TABLES = (
    "org_avatars",
    "org_avatar_versions",
    "org_avatar_assignments",
    "org_avatar_audit",
)


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE public.org_avatars (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          avatar_key text NOT NULL,
          enabled boolean NOT NULL DEFAULT true,
          current_version integer NOT NULL DEFAULT 0,
          created_by text NOT NULL DEFAULT '',
          updated_by text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, avatar_key)
        );

        CREATE TABLE public.org_avatar_versions (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          avatar_key text NOT NULL,
          version integer NOT NULL,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft', 'published', 'archived')),
          overlay_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          change_note text NOT NULL DEFAULT '',
          version_token uuid NOT NULL DEFAULT gen_random_uuid(),
          created_by text NOT NULL DEFAULT '',
          published_by text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          published_at timestamptz,
          PRIMARY KEY (org_id, avatar_key, version),
          FOREIGN KEY (org_id, avatar_key)
            REFERENCES public.org_avatars(org_id, avatar_key)
            ON DELETE CASCADE
        );
        -- Exactly one editable draft per (org, avatar): edits converge on it,
        -- publish archives it into an immutable row.
        CREATE UNIQUE INDEX uq_org_avatar_versions_one_draft
          ON public.org_avatar_versions(org_id, avatar_key)
          WHERE status = 'draft';
        CREATE INDEX idx_org_avatar_versions_history
          ON public.org_avatar_versions(org_id, avatar_key, version DESC);

        CREATE TABLE public.org_avatar_assignments (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          avatar_key text NOT NULL,
          scope_kind text NOT NULL
            CHECK (scope_kind IN ('org_default', 'user')),
          scope_value text NOT NULL DEFAULT '',
          priority integer NOT NULL DEFAULT 0,
          active boolean NOT NULL DEFAULT true,
          created_by text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          FOREIGN KEY (org_id, avatar_key)
            REFERENCES public.org_avatars(org_id, avatar_key)
            ON DELETE CASCADE
        );
        -- One assignment per (scope kind, scope value): the org default is
        -- the single ('org_default','') row; a user has one row.
        CREATE UNIQUE INDEX uq_org_avatar_assignments_scope
          ON public.org_avatar_assignments(org_id, scope_kind, scope_value)
          WHERE active;

        CREATE TABLE public.org_avatar_audit (
          id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          avatar_key text NOT NULL DEFAULT '',
          action text NOT NULL,
          actor text NOT NULL DEFAULT '',
          detail_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_org_avatar_audit_org
          ON public.org_avatar_audit(org_id, at DESC, id DESC);
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
        REVOKE ALL ON TABLE
          public.org_avatars, public.org_avatar_versions,
          public.org_avatar_assignments, public.org_avatar_audit
        FROM PUBLIC;
        REVOKE ALL ON SEQUENCE public.org_avatar_audit_id_seq FROM PUBLIC;

        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE
              public.org_avatars, public.org_avatar_versions,
              public.org_avatar_assignments, public.org_avatar_audit
            FROM anon;
            REVOKE ALL ON SEQUENCE public.org_avatar_audit_id_seq FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE
              public.org_avatars, public.org_avatar_versions,
              public.org_avatar_assignments, public.org_avatar_audit
            FROM authenticated;
            REVOKE ALL ON SEQUENCE
              public.org_avatar_audit_id_seq FROM authenticated;
          END IF;
        END
        $roles$;

        REVOKE ALL ON TABLE
          public.org_avatars, public.org_avatar_versions,
          public.org_avatar_assignments, public.org_avatar_audit
        FROM laura_app;
        REVOKE ALL ON SEQUENCE public.org_avatar_audit_id_seq FROM laura_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE
          public.org_avatars, public.org_avatar_versions,
          public.org_avatar_assignments TO laura_app;
        -- Audit is APPEND-ONLY by grant: INSERT+SELECT, never UPDATE/DELETE.
        GRANT SELECT, INSERT ON TABLE public.org_avatar_audit TO laura_app;
        GRANT USAGE ON SEQUENCE public.org_avatar_audit_id_seq TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0011 is the org-avatar personalization boundary; write a reviewed "
        "forward migration instead of dropping customer configuration."
    )
