"""Meeting Memory — durable per-org recall of finalized meetings.

Revision ID: 0025_meeting_memory
Revises: 0024_graph_connector
Create Date: 2026-07-29

A searchable, permission-safe index of FINALIZED meeting artifacts, holding
ONLY distilled fields — title, date, avatar, participants (display names),
summary, decisions, actions, and the stable meeting id (the bot id). The raw
transcript is DELIBERATELY absent: there is no transcript column, so the memory
index cannot leak it by construction — hard constraint 6 stays intact while
still letting an avatar recall what happened across authorized past meetings.

``visibility`` mirrors the artifacts contract (participants|org|private) so
search can default-deny: a caller sees a meeting only when it is org-visible or
they ran it (participants/private collapse to the runner for search — a
transcript display name is not an identity and never grants access).

Same discipline as 0010/0012: FORCE RLS with the NULLIF ``app.current_org``
policy TO laura_app, REVOKE-then-GRANT least privilege, and a generated
tsvector + GIN index for the keyword half of search. DELETE is granted because
this table is a DERIVED PROJECTION of the artifact — re-finalize upserts, and
deletion is the revocation primitive, always rebuildable from the artifact.
Downgrade raises: like every durable-data boundary here, narrowing is a
reviewed forward migration, never a silent drop.
"""
from __future__ import annotations

from alembic import op

revision = "0025_meeting_memory"
down_revision = "0024_graph_connector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE public.meeting_memory (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          meeting_id text NOT NULL,
          avatar_id text NOT NULL DEFAULT '',
          title text NOT NULL DEFAULT '',
          meeting_date timestamptz,
          participants jsonb NOT NULL DEFAULT '[]'::jsonb,
          summary text NOT NULL DEFAULT '',
          decisions jsonb NOT NULL DEFAULT '[]'::jsonb,
          actions jsonb NOT NULL DEFAULT '[]'::jsonb,
          visibility text NOT NULL DEFAULT 'participants'
            CHECK (visibility IN ('participants', 'org', 'private')),
          principal_id text NOT NULL DEFAULT '',
          search_text text NOT NULL DEFAULT '',
          fts tsvector GENERATED ALWAYS AS (
            to_tsvector('simple', search_text)
          ) STORED,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, meeting_id)
        );
        CREATE INDEX idx_meeting_memory_fts
          ON public.meeting_memory USING GIN (fts);
        CREATE INDEX idx_meeting_memory_recent
          ON public.meeting_memory(org_id, meeting_date DESC, meeting_id);

        ALTER TABLE public.meeting_memory ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.meeting_memory FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON public.meeting_memory
          TO laura_app
          USING (
            org_id = NULLIF(current_setting('app.current_org', true), '')::uuid
          )
          WITH CHECK (
            org_id = NULLIF(current_setting('app.current_org', true), '')::uuid
          );

        REVOKE ALL ON TABLE public.meeting_memory FROM PUBLIC;
        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE public.meeting_memory FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE public.meeting_memory FROM authenticated;
          END IF;
        END
        $roles$;
        REVOKE ALL ON TABLE public.meeting_memory FROM laura_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
          public.meeting_memory TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0025 is the Meeting Memory boundary; write a reviewed forward "
        "migration instead of dropping customer meeting recall."
    )
