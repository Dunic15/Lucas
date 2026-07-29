"""Meeting Memory facets — structured filters over meeting records.

Revision ID: 0025_meeting_memory_facets
Revises: 0024_df_msgraph_meeting_kinds
Create Date: 2026-07-29

Meeting Memory must answer "what did we decide with Acme in the last six
months", "which commitments to Sarah are open", "recurring risks on this
project". Those need filters on participant / customer / project / topic /
date, which DF heads do not carry.

This table is a pure PROJECTION of the distilled meeting, keyed by the DF
record it belongs to. It is NOT an access-control surface and never widens
visibility: every query joins it against the ACL-filtered head set produced
by ``dal.visible_heads``, so a facet row for a record the caller cannot see
contributes nothing. Rows are replaced wholesale whenever the meeting is
re-indexed, and cascade away with the record.

Same tenancy discipline as every other tenant table: composite PK on
(org_id, ...), FORCE RLS with the tenant_isolation policy, REVOKE-then-GRANT
to laura_app only.
"""
from __future__ import annotations

from alembic import op

revision = "0025_meeting_memory_facets"
down_revision = "0024_df_msgraph_meeting_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE public.df_meeting_facets (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          record_id uuid NOT NULL,
          meeting_id text NOT NULL,
          title text NOT NULL DEFAULT '',
          platform text NOT NULL DEFAULT '',
          customer text NOT NULL DEFAULT '',
          project text NOT NULL DEFAULT '',
          series_key text NOT NULL DEFAULT '',
          occurred_at timestamptz,
          participants_json text NOT NULL DEFAULT '[]',
          topics_json text NOT NULL DEFAULT '[]',
          open_actions integer NOT NULL DEFAULT 0,
          indexed_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, record_id),
          FOREIGN KEY (org_id, record_id)
            REFERENCES public.df_source_records(org_id, id) ON DELETE CASCADE
        );
        CREATE INDEX idx_df_meeting_facets_when
          ON public.df_meeting_facets(org_id, occurred_at DESC);
        CREATE INDEX idx_df_meeting_facets_customer
          ON public.df_meeting_facets(org_id, customer)
          WHERE customer <> '';
        CREATE INDEX idx_df_meeting_facets_project
          ON public.df_meeting_facets(org_id, project)
          WHERE project <> '';
        CREATE INDEX idx_df_meeting_facets_series
          ON public.df_meeting_facets(org_id, series_key)
          WHERE series_key <> '';

        ALTER TABLE public.df_meeting_facets ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.df_meeting_facets FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON public.df_meeting_facets
          TO laura_app
          USING (
            org_id = NULLIF(current_setting('app.current_org', true), '')::uuid
          )
          WITH CHECK (
            org_id = NULLIF(current_setting('app.current_org', true), '')::uuid
          );

        REVOKE ALL ON TABLE public.df_meeting_facets FROM PUBLIC;

        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE public.df_meeting_facets FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE public.df_meeting_facets FROM authenticated;
          END IF;
        END
        $roles$;

        REVOKE ALL ON TABLE public.df_meeting_facets FROM laura_app;
        -- Facets are derived and wholesale-replaced on every re-index.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
          public.df_meeting_facets TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0025 adds a derived projection; drop it only through a reviewed "
        "forward migration."
    )
