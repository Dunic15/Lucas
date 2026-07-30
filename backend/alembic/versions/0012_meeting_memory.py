"""Meeting Memory Slice 1: cross-meeting accumulating memory (distilled only).

Revision ID: 0012_meeting_memory
Revises: 0011_company_brain_graph
Create Date: 2026-07-30

docs/company-brain/MEETING-MEMORY-SPEC.md. Every finalized meeting deposits its
DISTILLED artifact fields (summary/decisions/actions/attendees — never a
transcript, never an utterance) into tenant-scoped memory tables, and a cached
past-week digest feeds the pre-meeting brief:

- ``memory_meetings``  — one row per finished meeting (bot_id), carrying the
                         series key (ledger.meeting_key) so "same recurring
                         meeting" stays a query, not a node.
- ``memory_entities``  — ONE node per real-world thing; the
                         UNIQUE (org_id, kind, key) constraint IS the
                         anti-duplicate invariant (person key = lowercased
                         email, else 'dn:' + normalized display name).
- ``memory_attendees`` — attendance edges meeting → person entity, with
                         entity-resolution provenance.
- ``memory_digests``   — the cached past-week working memory (one live digest
                         per (org, scope, avatar), refreshed in place).

``memory_edges`` (decided/owns/mentions/…) is deliberately Slice 2, together
with meeting-as-document ingestion into the knowledge plane and its ACL rows.
All four tables are the derived/replaceable grant tier (runtime may delete —
re-deposit and Slice-3 compaction depend on it).
"""
from __future__ import annotations

from alembic import op

revision = "0012_meeting_memory"
down_revision = "0011_company_brain_graph"
branch_labels = None
depends_on = None

_NEW_TENANT_TABLES = (
    "memory_meetings",
    "memory_entities",
    "memory_attendees",
    "memory_digests",
)


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE public.memory_meetings (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          bot_id text NOT NULL,
          meeting_key text NOT NULL DEFAULT '',
          avatar_id text NOT NULL DEFAULT '',
          title text NOT NULL DEFAULT '',
          meeting_type text NOT NULL DEFAULT '',
          started_at timestamptz,
          ended_at timestamptz,
          duration_seconds integer NOT NULL DEFAULT 0,
          readiness_score integer NOT NULL DEFAULT 0,
          summary text NOT NULL DEFAULT '',
          decisions_json text NOT NULL DEFAULT '[]',
          actions_json text NOT NULL DEFAULT '[]',
          missing_steps_json text NOT NULL DEFAULT '[]',
          -- Slice 2: FK (org_id, document_id) -> knowledge_documents once the
          -- meeting-as-document ingestion lands; plain column until then.
          document_id uuid,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          UNIQUE (org_id, bot_id)
        );
        CREATE INDEX idx_memory_meetings_window
          ON public.memory_meetings(org_id, ended_at DESC);
        CREATE INDEX idx_memory_meetings_series
          ON public.memory_meetings(org_id, meeting_key, ended_at DESC);

        CREATE TABLE public.memory_entities (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          kind text NOT NULL CHECK (kind IN
            ('person', 'project', 'document', 'decision', 'action', 'meeting')),
          key text NOT NULL,
          display text NOT NULL DEFAULT '',
          email text NOT NULL DEFAULT '',
          ref_kind text NOT NULL DEFAULT '',
          ref_id text NOT NULL DEFAULT '',
          alias_of uuid,
          first_seen_at timestamptz NOT NULL DEFAULT now(),
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          mention_count integer NOT NULL DEFAULT 1,
          PRIMARY KEY (org_id, id),
          UNIQUE (org_id, kind, key),
          FOREIGN KEY (org_id, alias_of)
            REFERENCES public.memory_entities(org_id, id)
        );
        CREATE INDEX idx_memory_entities_recent
          ON public.memory_entities(org_id, kind, last_seen_at DESC);

        CREATE TABLE public.memory_attendees (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          meeting_id uuid NOT NULL,
          entity_id uuid NOT NULL,
          display text NOT NULL DEFAULT '',
          email text NOT NULL DEFAULT '',
          resolution text NOT NULL DEFAULT 'display_name'
            CHECK (resolution IN ('email', 'directory', 'display_name')),
          spoke boolean NOT NULL DEFAULT false,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, meeting_id, entity_id),
          FOREIGN KEY (org_id, meeting_id)
            REFERENCES public.memory_meetings(org_id, id) ON DELETE CASCADE,
          FOREIGN KEY (org_id, entity_id)
            REFERENCES public.memory_entities(org_id, id) ON DELETE CASCADE
        );
        CREATE INDEX idx_memory_attendees_entity
          ON public.memory_attendees(org_id, entity_id);

        CREATE TABLE public.memory_digests (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          scope text NOT NULL DEFAULT 'avatar'
            CHECK (scope IN ('org', 'avatar')),
          avatar_id text NOT NULL DEFAULT '',
          window_start timestamptz,
          window_end timestamptz,
          text text NOT NULL DEFAULT '',
          source_meeting_ids_json text NOT NULL DEFAULT '[]',
          model text NOT NULL DEFAULT '',
          generated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          UNIQUE (org_id, scope, avatar_id)
        );
        """
    )

    for table in _NEW_TENANT_TABLES:
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
          public.memory_meetings,
          public.memory_entities,
          public.memory_attendees,
          public.memory_digests
        FROM PUBLIC;

        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE
              public.memory_meetings, public.memory_entities,
              public.memory_attendees, public.memory_digests
            FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE
              public.memory_meetings, public.memory_entities,
              public.memory_attendees, public.memory_digests
            FROM authenticated;
          END IF;
        END
        $roles$;

        REVOKE ALL ON TABLE
          public.memory_meetings, public.memory_entities,
          public.memory_attendees, public.memory_digests
        FROM laura_app;
        -- Derived, replaceable memory: re-deposit upserts, digest refreshes in
        -- place, and Slice-3 compaction deletes — full CRUD, unlike the
        -- tombstone-only document/version tier.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
          public.memory_meetings, public.memory_entities,
          public.memory_attendees, public.memory_digests TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0012 holds accumulated meeting memory; write a reviewed forward "
        "migration instead of dropping it."
    )
