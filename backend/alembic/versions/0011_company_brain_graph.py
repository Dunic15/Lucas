"""Company Brain M2: connector sync state, normalized ACLs, audit (enterprise data plane).

Revision ID: 0011_company_brain_graph
Revises: 0010_company_brain
Create Date: 2026-07-29

M1 (0010) gave the durable knowledge plane org isolation, versions, chunks and
jobs. M2 makes it an enterprise DATA PLANE for external sources — Microsoft
Graph first, contract source-agnostic:

- ``knowledge_sources``   + connector columns: config_json (non-secret scope
                          config — NEVER tokens), connection_status,
                          last_sync_at/last_sync_error. kind gains 'msgraph'.
- ``knowledge_documents`` + canonical-item columns: external_id (stable source
                          item id — the identity for connector documents),
                          title, web_url, source_version (eTag/cTag),
                          source_modified_at, author, parent_ref (container
                          hierarchy), acl_synced_at/acl_error (permission
                          freshness — stale ⇒ deny at query time).
- ``knowledge_chunks``    + embedding_json (provider-stamped per-chunk vector;
                          the semantic half of query-time hybrid retrieval).
- ``knowledge_principals``     — normalized source principals (user/group/
                                 domain/tenant/everyone/link), per connection.
- ``knowledge_group_edges``    — DIRECT membership edges; expanded recursively
                                 at query time (depth-capped in the DAL).
- ``knowledge_document_acl``   — per-document grants with inheritance lineage;
                                 replaced atomically on each permission fetch.
- ``knowledge_identity_map``   — Laura caller (lowercased email) → source
                                 principal. No mapping ⇒ empty principal set
                                 ⇒ default deny.
- ``knowledge_sync_state``     — durable opaque checkpoints (Graph delta/next
                                 links) per (source, resource).
- ``knowledge_audit``          — append-only data-plane audit (syncs, queries,
                                 denials, tombstones, revocations). No content,
                                 no raw query text (hashes only).

Job kinds gain 'connector_sync'. DELETE grants extend to the derived/
replaceable tables only (principals, edges, acl, identity, sync_state) — the
document/version record stays tombstone-only, and audit is INSERT+SELECT.
"""
from __future__ import annotations

from alembic import op

revision = "0011_company_brain_graph"
down_revision = "0010_company_brain"
branch_labels = None
depends_on = None

_NEW_TENANT_TABLES = (
    "knowledge_principals",
    "knowledge_group_edges",
    "knowledge_document_acl",
    "knowledge_identity_map",
    "knowledge_sync_state",
    "knowledge_audit",
)


def upgrade() -> None:
    # ── widen the CHECK-constrained vocabularies (0009's DROP/re-ADD move) ──
    op.execute(
        r"""
        ALTER TABLE public.knowledge_sources
          DROP CONSTRAINT IF EXISTS knowledge_sources_kind_check;
        ALTER TABLE public.knowledge_sources
          ADD CONSTRAINT knowledge_sources_kind_check
          CHECK (kind IN ('upload', 'drive', 'msgraph'));

        ALTER TABLE public.knowledge_sync_jobs
          DROP CONSTRAINT IF EXISTS knowledge_sync_jobs_kind_check;
        ALTER TABLE public.knowledge_sync_jobs
          ADD CONSTRAINT knowledge_sync_jobs_kind_check
          CHECK (kind IN (
            'ingest_document', 'rebuild_index', 'sync_drive', 'connector_sync'
          ));
        """
    )

    # ── connector columns on existing tables ──
    op.execute(
        r"""
        ALTER TABLE public.knowledge_sources
          ADD COLUMN config_json text NOT NULL DEFAULT '{}',
          ADD COLUMN connection_status text NOT NULL DEFAULT 'active'
            CHECK (connection_status IN
                   ('pending_scope', 'active', 'revoked', 'error')),
          ADD COLUMN last_sync_at timestamptz,
          ADD COLUMN last_sync_error text NOT NULL DEFAULT '';

        ALTER TABLE public.knowledge_documents
          ADD COLUMN external_id text NOT NULL DEFAULT '',
          ADD COLUMN title text NOT NULL DEFAULT '',
          ADD COLUMN web_url text NOT NULL DEFAULT '',
          ADD COLUMN source_version text NOT NULL DEFAULT '',
          ADD COLUMN source_modified_at timestamptz,
          ADD COLUMN author text NOT NULL DEFAULT '',
          ADD COLUMN parent_ref text NOT NULL DEFAULT '',
          ADD COLUMN acl_synced_at timestamptz,
          ADD COLUMN acl_error text NOT NULL DEFAULT '';
        CREATE UNIQUE INDEX uq_knowledge_documents_external
          ON public.knowledge_documents(org_id, source_id, external_id)
          WHERE external_id <> '';

        ALTER TABLE public.knowledge_chunks
          ADD COLUMN embedding_json text NOT NULL DEFAULT '';
        """
    )

    # ── new tables ──
    op.execute(
        r"""
        CREATE TABLE public.knowledge_principals (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          source_id uuid NOT NULL,
          external_id text NOT NULL,
          kind text NOT NULL CHECK (kind IN
            ('user', 'group', 'domain', 'tenant', 'everyone', 'link')),
          display text NOT NULL DEFAULT '',
          email text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          UNIQUE (org_id, source_id, kind, external_id),
          FOREIGN KEY (org_id, source_id)
            REFERENCES public.knowledge_sources(org_id, id) ON DELETE CASCADE
        );

        CREATE TABLE public.knowledge_group_edges (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          source_id uuid NOT NULL,
          group_id uuid NOT NULL,
          member_id uuid NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, group_id, member_id),
          FOREIGN KEY (org_id, source_id)
            REFERENCES public.knowledge_sources(org_id, id) ON DELETE CASCADE,
          FOREIGN KEY (org_id, group_id)
            REFERENCES public.knowledge_principals(org_id, id)
            ON DELETE CASCADE,
          FOREIGN KEY (org_id, member_id)
            REFERENCES public.knowledge_principals(org_id, id)
            ON DELETE CASCADE
        );
        CREATE INDEX idx_knowledge_group_edges_member
          ON public.knowledge_group_edges(org_id, member_id);

        CREATE TABLE public.knowledge_document_acl (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          document_id uuid NOT NULL,
          principal_id uuid NOT NULL,
          role text NOT NULL DEFAULT 'read',
          inherited_from text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, document_id, principal_id),
          FOREIGN KEY (org_id, document_id)
            REFERENCES public.knowledge_documents(org_id, id)
            ON DELETE CASCADE,
          FOREIGN KEY (org_id, principal_id)
            REFERENCES public.knowledge_principals(org_id, id)
            ON DELETE CASCADE
        );
        CREATE INDEX idx_knowledge_document_acl_principal
          ON public.knowledge_document_acl(org_id, principal_id);

        CREATE TABLE public.knowledge_identity_map (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          source_id uuid NOT NULL,
          user_key text NOT NULL,
          principal_id uuid NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, source_id, user_key),
          FOREIGN KEY (org_id, source_id)
            REFERENCES public.knowledge_sources(org_id, id) ON DELETE CASCADE,
          FOREIGN KEY (org_id, principal_id)
            REFERENCES public.knowledge_principals(org_id, id)
            ON DELETE CASCADE
        );

        CREATE TABLE public.knowledge_sync_state (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          source_id uuid NOT NULL,
          resource_key text NOT NULL,
          checkpoint text NOT NULL DEFAULT '',
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, source_id, resource_key),
          FOREIGN KEY (org_id, source_id)
            REFERENCES public.knowledge_sources(org_id, id) ON DELETE CASCADE
        );

        CREATE TABLE public.knowledge_audit (
          id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          ts timestamptz NOT NULL DEFAULT now(),
          actor text NOT NULL DEFAULT '',
          event text NOT NULL,
          detail_json text NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_knowledge_audit_org
          ON public.knowledge_audit(org_id, ts DESC, id DESC);
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
          public.knowledge_principals,
          public.knowledge_group_edges,
          public.knowledge_document_acl,
          public.knowledge_identity_map,
          public.knowledge_sync_state,
          public.knowledge_audit
        FROM PUBLIC;
        REVOKE ALL ON SEQUENCE public.knowledge_audit_id_seq FROM PUBLIC;

        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE
              public.knowledge_principals, public.knowledge_group_edges,
              public.knowledge_document_acl, public.knowledge_identity_map,
              public.knowledge_sync_state, public.knowledge_audit
            FROM anon;
            REVOKE ALL ON SEQUENCE public.knowledge_audit_id_seq FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE
              public.knowledge_principals, public.knowledge_group_edges,
              public.knowledge_document_acl, public.knowledge_identity_map,
              public.knowledge_sync_state, public.knowledge_audit
            FROM authenticated;
            REVOKE ALL ON SEQUENCE
              public.knowledge_audit_id_seq FROM authenticated;
          END IF;
        END
        $roles$;

        REVOKE ALL ON TABLE
          public.knowledge_principals, public.knowledge_group_edges,
          public.knowledge_document_acl, public.knowledge_identity_map,
          public.knowledge_sync_state, public.knowledge_audit
        FROM laura_app;
        REVOKE ALL ON SEQUENCE public.knowledge_audit_id_seq FROM laura_app;
        -- Derived, wholesale-replaceable data may be deleted by the runtime
        -- (ACL replace, membership refresh, revocation cleanup). Documents/
        -- versions stay tombstone-only (0010), audit is append-only.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
          public.knowledge_principals, public.knowledge_group_edges,
          public.knowledge_document_acl, public.knowledge_identity_map,
          public.knowledge_sync_state TO laura_app;
        GRANT SELECT, INSERT ON TABLE public.knowledge_audit TO laura_app;
        GRANT USAGE ON SEQUENCE public.knowledge_audit_id_seq TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0011 extends the durable Company Brain; write a reviewed forward "
        "migration instead of dropping ACL/sync state."
    )
