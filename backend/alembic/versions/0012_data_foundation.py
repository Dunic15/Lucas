"""Company Data Foundation DF0-DF1 (accepted contract hsk_con_nqrcynzgg746jrhkq647 v5).

Revision ID: 0012_data_foundation
Revises: 0011_org_avatars
Create Date: 2026-07-17

Implements the v5 schema verbatim plus the six binding acceptance
clarifications. Highlights:

- ``df_source_records`` is STABLE record identity + head state only;
  ``df_source_record_versions`` holds immutable content/metadata versions
  (supersession is never deletion). The circular head pointer is a DEFERRABLE
  composite FK added after both tables exist.
- ``acl_mode`` defaults to ``unknown``: FAIL-CLOSED: such records are
  visible to no principal and absent from every live index until a sync
  supplies real ACL.
- ``df_connector_cursors`` is the COMMITTED incremental cursor, separate from
  run history, advanced only inside the transaction that commits an accepted
  batch.
- ``df_quarantine`` open rows are UNDELETABLE by the runtime: laura_app gets
  no DELETE grant; the only purge path is ``laura_private.purge_quarantine``
  - SECURITY DEFINER, fixed search_path, PUBLIC revoked, org VERIFIED against
  the trusted transaction context (the argument cannot authorize cross-org
  deletion), retention cutoff CLAMPED server-side from the org's policy (a
  future cutoff cannot delete young rows), fixed state predicates
  (replayed|discarded only), and an org-scoped ``df_purge_audit`` row written
  atomically with the row deletion. External payload deletion is the
  documented retryable TWO-PHASE flow (audit row holds pending refs until a
  worker confirms); never treated as transactionally atomic with PostgreSQL.
- ``df_acl_entries`` uses a surrogate PK + two partial unique indexes (the
  legal-keys correction) with a typed subject.

Deployment order (binding): alembic 0012 -> DATA_FOUNDATION_ENABLED=true ->
upload backfill -> per-org Drive opt-in. Rollback is flag-off; this migration
is additive-only and downgrade raises.
"""
from __future__ import annotations

from alembic import op

revision = "0012_data_foundation"
down_revision = "0011_org_avatars"
branch_labels = None
depends_on = None

_TENANT_TABLES = (
    "df_connectors",
    "df_source_records",
    "df_source_record_versions",
    "df_connector_cursors",
    "df_sync_runs",
    "df_quarantine",
    "df_purge_audit",
    "df_identities",
    "df_principal_bindings",
    "df_identity_edges",
    "df_acl_entries",
)


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE public.df_connectors (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          kind text NOT NULL CHECK (
            kind IN ('upload', 'gdrive', 'slack', 'notion', 'crm', 'custom')
          ),
          name text NOT NULL,
          status text NOT NULL DEFAULT 'active' CHECK (
            status IN ('active', 'paused', 'needs_reconnect',
                       'acl_incomplete', 'revoked')
          ),
          config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          credential_ref text NOT NULL DEFAULT '',
          trusted_email_issuer boolean NOT NULL DEFAULT false,
          acl_mirrored boolean NOT NULL DEFAULT false,
          created_by text NOT NULL DEFAULT '',
          updated_by text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id)
        );

        CREATE TABLE public.df_source_records (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          connector_id uuid NOT NULL,
          external_id text NOT NULL,
          kind text NOT NULL
            CHECK (kind IN ('document', 'message', 'event', 'record')),
          container_external_id text NOT NULL DEFAULT '',
          tombstoned boolean NOT NULL DEFAULT false,
          current_version_id uuid,
          acl_mode text NOT NULL DEFAULT 'unknown'
            CHECK (acl_mode IN ('org_default', 'mirrored', 'unknown')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          FOREIGN KEY (org_id, connector_id)
            REFERENCES public.df_connectors(org_id, id) ON DELETE CASCADE,
          UNIQUE (org_id, connector_id, external_id)
        );
        CREATE INDEX idx_df_source_records_container
          ON public.df_source_records(org_id, connector_id,
                                      container_external_id);

        CREATE TABLE public.df_source_record_versions (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          record_id uuid NOT NULL,
          version_no integer NOT NULL,
          title text NOT NULL DEFAULT '',
          mime text NOT NULL DEFAULT '',
          canonical_url text NOT NULL DEFAULT '',
          author_external_id text NOT NULL DEFAULT '',
          body_checksum text NOT NULL DEFAULT '',
          body_ref text NOT NULL DEFAULT '',
          external_updated_at timestamptz,
          lineage_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          ingested_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          FOREIGN KEY (org_id, record_id)
            REFERENCES public.df_source_records(org_id, id) ON DELETE CASCADE,
          UNIQUE (org_id, record_id, version_no)
        );

        -- The circular head pointer, DEFERRABLE so head-swaps commit in one
        -- transaction. Composite with org_id: a cross-org version reference
        -- is impossible at the database (blocker test).
        ALTER TABLE public.df_source_records
          ADD CONSTRAINT fk_df_records_current_version
          FOREIGN KEY (org_id, current_version_id)
          REFERENCES public.df_source_record_versions(org_id, id)
          DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE public.df_connector_cursors (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          connector_id uuid NOT NULL,
          cursor_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          advanced_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, connector_id),
          FOREIGN KEY (org_id, connector_id)
            REFERENCES public.df_connectors(org_id, id) ON DELETE CASCADE
        );

        CREATE TABLE public.df_sync_runs (
          id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          connector_id uuid NOT NULL,
          kind text NOT NULL DEFAULT 'incremental'
            CHECK (kind IN ('full', 'incremental')),
          status text NOT NULL DEFAULT 'pending' CHECK (
            status IN ('pending', 'running', 'done', 'failed', 'parked',
                       'dead_letter')
          ),
          stats_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
          parks integer NOT NULL DEFAULT 0 CHECK (parks >= 0),
          next_attempt_at timestamptz DEFAULT now(),
          lease_token uuid,
          lease_until timestamptz,
          last_error text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY (org_id, connector_id)
            REFERENCES public.df_connectors(org_id, id) ON DELETE CASCADE
        );
        CREATE INDEX idx_df_sync_runs_due
          ON public.df_sync_runs(next_attempt_at, id)
          WHERE status IN ('pending', 'failed') AND next_attempt_at IS NOT NULL;
        CREATE INDEX idx_df_sync_runs_lease
          ON public.df_sync_runs(lease_until, id)
          WHERE status = 'running';
        CREATE INDEX idx_df_sync_runs_org
          ON public.df_sync_runs(org_id, connector_id, created_at DESC);

        CREATE TABLE public.df_quarantine (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          connector_id uuid NOT NULL,
          sync_run_id bigint,
          external_id text NOT NULL DEFAULT '',
          reason text NOT NULL,
          payload_ref text NOT NULL DEFAULT '',
          state text NOT NULL DEFAULT 'open'
            CHECK (state IN ('open', 'replayed', 'discarded')),
          created_at timestamptz NOT NULL DEFAULT now(),
          resolved_at timestamptz,
          resolved_by text NOT NULL DEFAULT '',
          PRIMARY KEY (org_id, id),
          FOREIGN KEY (org_id, connector_id)
            REFERENCES public.df_connectors(org_id, id) ON DELETE CASCADE
        );
        CREATE INDEX idx_df_quarantine_open
          ON public.df_quarantine(org_id, connector_id, created_at)
          WHERE state = 'open';

        -- Two-phase purge audit: the definer function inserts the row (with
        -- the payload refs still pending) atomically with the row deletion;
        -- the retention worker deletes the storage objects and marks the row
        -- completed; retry-safe, never assumed atomic with PostgreSQL.
        CREATE TABLE public.df_purge_audit (
          id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          purged_count integer NOT NULL DEFAULT 0,
          cutoff_used timestamptz NOT NULL,
          payload_refs_pending jsonb NOT NULL DEFAULT '[]'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz
        );
        CREATE INDEX idx_df_purge_audit_pending
          ON public.df_purge_audit(org_id, id)
          WHERE completed_at IS NULL;

        CREATE TABLE public.df_identities (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          connector_id uuid NOT NULL,
          external_id text NOT NULL,
          kind text NOT NULL CHECK (kind IN ('user', 'group')),
          display text NOT NULL DEFAULT '',
          email_norm text NOT NULL DEFAULT '',
          mirrored_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          FOREIGN KEY (org_id, connector_id)
            REFERENCES public.df_connectors(org_id, id) ON DELETE CASCADE,
          UNIQUE (org_id, connector_id, external_id)
        );

        CREATE TABLE public.df_principal_bindings (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          identity_id uuid NOT NULL,
          principal_ref text NOT NULL,
          method text NOT NULL
            CHECK (method IN ('explicit_admin', 'verified_email')),
          bound_by text NOT NULL DEFAULT '',
          bound_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, identity_id),
          FOREIGN KEY (org_id, identity_id)
            REFERENCES public.df_identities(org_id, id) ON DELETE CASCADE
        );
        CREATE INDEX idx_df_bindings_principal
          ON public.df_principal_bindings(org_id, principal_ref);

        CREATE TABLE public.df_identity_edges (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          group_id uuid NOT NULL,
          member_id uuid NOT NULL,
          mirrored_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, group_id, member_id),
          FOREIGN KEY (org_id, group_id)
            REFERENCES public.df_identities(org_id, id) ON DELETE CASCADE,
          FOREIGN KEY (org_id, member_id)
            REFERENCES public.df_identities(org_id, id) ON DELETE CASCADE
        );

        CREATE TABLE public.df_acl_entries (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          record_id uuid NOT NULL,
          subject_kind text NOT NULL CHECK (subject_kind IN ('org', 'identity')),
          identity_id uuid,
          access text NOT NULL DEFAULT 'reader'
            CHECK (access IN ('reader', 'writer', 'owner')),
          mirrored_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          CHECK ((subject_kind = 'org') = (identity_id IS NULL)),
          FOREIGN KEY (org_id, record_id)
            REFERENCES public.df_source_records(org_id, id) ON DELETE CASCADE,
          FOREIGN KEY (org_id, identity_id)
            REFERENCES public.df_identities(org_id, id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX uq_df_acl_org_subject
          ON public.df_acl_entries(org_id, record_id)
          WHERE subject_kind = 'org';
        CREATE UNIQUE INDEX uq_df_acl_identity_subject
          ON public.df_acl_entries(org_id, record_id, identity_id)
          WHERE subject_kind = 'identity';
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
        CREATE OR REPLACE FUNCTION laura_private.due_df_orgs(
          p_limit integer DEFAULT 20
        )
        RETURNS SETOF uuid
        LANGUAGE sql
        VOLATILE
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
          SELECT r.org_id
            FROM public.df_sync_runs AS r
           WHERE (
             r.status IN ('pending', 'failed')
             AND r.next_attempt_at IS NOT NULL
             AND r.next_attempt_at <= pg_catalog.clock_timestamp()
           ) OR (
             r.status = 'running'
             AND r.lease_until IS NOT NULL
             AND r.lease_until <= pg_catalog.clock_timestamp()
           )
           GROUP BY r.org_id
           ORDER BY min(
             CASE WHEN r.status = 'running'
                  THEN r.lease_until ELSE r.next_attempt_at END
           ), r.org_id
           LIMIT LEAST(GREATEST(COALESCE(p_limit, 20), 1), 100)
        $function$;

        -- THE ONLY quarantine RESOLUTION path: laura_app has no direct
        -- UPDATE on df_quarantine (see grants), so it cannot backdate
        -- resolved_at to fast-track a purge. This definer stamps resolved_at
        -- = clock_timestamp() SERVER-SIDE, verifies the org against the
        -- transaction context, and only transitions OPEN -> replayed|
        -- discarded. A just-resolved row is therefore always young and the
        -- retention clamp genuinely protects it for the full window.
        CREATE OR REPLACE FUNCTION laura_private.resolve_quarantine(
          p_org_id uuid,
          p_qid uuid,
          p_state text,
          p_actor text DEFAULT ''
        )
        RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
        DECLARE
          v_ctx uuid;
          v_count integer;
        BEGIN
          v_ctx := NULLIF(
            pg_catalog.current_setting('app.current_org', true), ''
          )::uuid;
          IF v_ctx IS NULL OR p_org_id IS DISTINCT FROM v_ctx THEN
            RAISE EXCEPTION
              'resolve_quarantine: org does not match transaction context';
          END IF;
          IF p_state NOT IN ('replayed', 'discarded') THEN
            RAISE EXCEPTION 'resolve_quarantine: bad state';
          END IF;
          WITH updated AS (
            UPDATE public.df_quarantine q
            SET state = p_state,
                resolved_at = pg_catalog.clock_timestamp(),
                resolved_by = LEFT(COALESCE(p_actor, ''), 64)
            WHERE q.org_id = p_org_id AND q.id = p_qid AND q.state = 'open'
            RETURNING 1
          )
          SELECT pg_catalog.count(*) INTO v_count FROM updated;
          RETURN v_count;
        END
        $function$;

        -- THE ONLY quarantine purge path (acceptance clarifications 1-4):
        -- org VERIFIED against the trusted transaction context (the argument
        -- cannot authorize cross-org deletion); cutoff CLAMPED server-side by
        -- the org's retention policy so a future cutoff cannot reach young
        -- rows; fixed predicates purge ONLY resolved rows; the audit row with
        -- pending payload refs commits atomically with the deletion.
        CREATE OR REPLACE FUNCTION laura_private.purge_quarantine(
          p_org_id uuid,
          p_older_than timestamptz DEFAULT NULL
        )
        RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
        DECLARE
          v_ctx uuid;
          v_retention_days integer;
          v_policy_cutoff timestamptz;
          v_cutoff timestamptz;
          v_refs jsonb;
          v_count integer;
        BEGIN
          v_ctx := NULLIF(
            pg_catalog.current_setting('app.current_org', true), ''
          )::uuid;
          IF v_ctx IS NULL OR p_org_id IS DISTINCT FROM v_ctx THEN
            RAISE EXCEPTION
              'purge_quarantine: org does not match transaction context';
          END IF;
          SELECT COALESCE(o.retention_days, 30) INTO v_retention_days
            FROM public.orgs o WHERE o.id = p_org_id;
          IF v_retention_days IS NULL THEN
            RAISE EXCEPTION 'purge_quarantine: unknown org';
          END IF;
          v_policy_cutoff := pg_catalog.clock_timestamp()
            - pg_catalog.make_interval(days => v_retention_days);
          v_cutoff := LEAST(COALESCE(p_older_than, v_policy_cutoff),
                            v_policy_cutoff);
          WITH purged AS (
            DELETE FROM public.df_quarantine q
            WHERE q.org_id = p_org_id
              AND q.state IN ('replayed', 'discarded')
              AND q.resolved_at IS NOT NULL
              AND q.resolved_at < v_cutoff
            RETURNING q.payload_ref
          )
          SELECT COALESCE(
                   pg_catalog.jsonb_agg(p.payload_ref)
                     FILTER (WHERE p.payload_ref <> ''),
                   '[]'::jsonb
                 ),
                 pg_catalog.count(*)
            INTO v_refs, v_count
            FROM purged p;
          IF v_count > 0 THEN
            INSERT INTO public.df_purge_audit
              (org_id, purged_count, cutoff_used, payload_refs_pending)
            VALUES (p_org_id, v_count, v_cutoff, v_refs);
          END IF;
          RETURN v_count;
        END
        $function$;

        -- Version-history purge: same discipline as quarantine; laura_app
        -- has no DELETE on df_source_record_versions; the ONLY deleter is
        -- this definer with org verification, server-clamped cutoff, and the
        -- fixed predicate "non-current versions only". Bodies purge via the
        -- same two-phase df_purge_audit flow.
        CREATE OR REPLACE FUNCTION laura_private.purge_record_versions(
          p_org_id uuid,
          p_older_than timestamptz DEFAULT NULL
        )
        RETURNS integer
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = ''
        AS $function$
        DECLARE
          v_ctx uuid;
          v_retention_days integer;
          v_policy_cutoff timestamptz;
          v_cutoff timestamptz;
          v_refs jsonb;
          v_count integer;
        BEGIN
          v_ctx := NULLIF(
            pg_catalog.current_setting('app.current_org', true), ''
          )::uuid;
          IF v_ctx IS NULL OR p_org_id IS DISTINCT FROM v_ctx THEN
            RAISE EXCEPTION
              'purge_record_versions: org does not match transaction context';
          END IF;
          SELECT COALESCE(o.retention_days, 30) INTO v_retention_days
            FROM public.orgs o WHERE o.id = p_org_id;
          IF v_retention_days IS NULL THEN
            RAISE EXCEPTION 'purge_record_versions: unknown org';
          END IF;
          v_policy_cutoff := pg_catalog.clock_timestamp()
            - pg_catalog.make_interval(days => v_retention_days);
          v_cutoff := LEAST(COALESCE(p_older_than, v_policy_cutoff),
                            v_policy_cutoff);
          WITH purged AS (
            DELETE FROM public.df_source_record_versions v
            WHERE v.org_id = p_org_id
              AND v.ingested_at < v_cutoff
              -- STRUCTURAL protection: never the highest version_no for a
              -- record. Immune to a runtime that NULLs current_version_id -
              -- the live version is always the max and is always kept.
              AND v.version_no < (
                SELECT pg_catalog.max(v2.version_no)
                FROM public.df_source_record_versions v2
                WHERE v2.org_id = v.org_id AND v2.record_id = v.record_id
              )
            RETURNING v.body_ref
          )
          SELECT COALESCE(
                   pg_catalog.jsonb_agg(p.body_ref)
                     FILTER (WHERE p.body_ref <> ''),
                   '[]'::jsonb
                 ),
                 pg_catalog.count(*)
            INTO v_refs, v_count
            FROM purged p;
          IF v_count > 0 THEN
            INSERT INTO public.df_purge_audit
              (org_id, purged_count, cutoff_used, payload_refs_pending)
            VALUES (p_org_id, v_count, v_cutoff, v_refs);
          END IF;
          RETURN v_count;
        END
        $function$;

        REVOKE ALL ON TABLE
          public.df_connectors, public.df_source_records,
          public.df_source_record_versions, public.df_connector_cursors,
          public.df_sync_runs, public.df_quarantine, public.df_purge_audit,
          public.df_identities, public.df_principal_bindings,
          public.df_identity_edges, public.df_acl_entries
        FROM PUBLIC;
        REVOKE ALL ON SEQUENCE
          public.df_sync_runs_id_seq, public.df_purge_audit_id_seq
        FROM PUBLIC;
        REVOKE ALL ON FUNCTION
          laura_private.due_df_orgs(integer),
          laura_private.purge_quarantine(uuid, timestamptz),
          laura_private.purge_record_versions(uuid, timestamptz),
          laura_private.resolve_quarantine(uuid, uuid, text, text)
        FROM PUBLIC;

        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE
              public.df_connectors, public.df_source_records,
              public.df_source_record_versions, public.df_connector_cursors,
              public.df_sync_runs, public.df_quarantine,
              public.df_purge_audit, public.df_identities,
              public.df_principal_bindings, public.df_identity_edges,
              public.df_acl_entries FROM anon;
            REVOKE ALL ON SEQUENCE
              public.df_sync_runs_id_seq,
              public.df_purge_audit_id_seq FROM anon;
            REVOKE ALL ON FUNCTION
              laura_private.due_df_orgs(integer),
              laura_private.purge_quarantine(uuid, timestamptz),
              laura_private.purge_record_versions(uuid, timestamptz),
              laura_private.resolve_quarantine(uuid, uuid, text, text) FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE
              public.df_connectors, public.df_source_records,
              public.df_source_record_versions, public.df_connector_cursors,
              public.df_sync_runs, public.df_quarantine,
              public.df_purge_audit, public.df_identities,
              public.df_principal_bindings, public.df_identity_edges,
              public.df_acl_entries FROM authenticated;
            REVOKE ALL ON SEQUENCE
              public.df_sync_runs_id_seq,
              public.df_purge_audit_id_seq FROM authenticated;
            REVOKE ALL ON FUNCTION
              laura_private.due_df_orgs(integer),
              laura_private.purge_quarantine(uuid, timestamptz),
              laura_private.purge_record_versions(uuid, timestamptz),
              laura_private.resolve_quarantine(uuid, uuid, text, text)
            FROM authenticated;
          END IF;
        END
        $roles$;

        REVOKE ALL ON TABLE
          public.df_connectors, public.df_source_records,
          public.df_source_record_versions, public.df_connector_cursors,
          public.df_sync_runs, public.df_quarantine, public.df_purge_audit,
          public.df_identities, public.df_principal_bindings,
          public.df_identity_edges, public.df_acl_entries
        FROM laura_app;
        REVOKE ALL ON SEQUENCE
          public.df_sync_runs_id_seq, public.df_purge_audit_id_seq
        FROM laura_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE
          public.df_connectors, public.df_source_records,
          public.df_source_record_versions, public.df_connector_cursors,
          public.df_sync_runs, public.df_identities,
          public.df_principal_bindings TO laura_app;
        -- Quarantine: NO DELETE and NO UPDATE for the runtime; open rows
        -- are structurally undeletable AND resolved_at is unforgeable.
        -- Resolution is EXECUTE on laura_private.resolve_quarantine; purge
        -- is EXECUTE on laura_private.purge_quarantine. Nothing else.
        GRANT SELECT, INSERT ON TABLE public.df_quarantine TO laura_app;
        -- Purge audit: INSERT happens inside the definer; the runtime worker
        -- reads pending rows and marks them completed (two-phase cleanup).
        GRANT SELECT, UPDATE ON TABLE public.df_purge_audit TO laura_app;
        -- Mirror tables: deletion IS the refresh primitive.
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
          public.df_identity_edges, public.df_acl_entries TO laura_app;
        GRANT USAGE ON SEQUENCE
          public.df_sync_runs_id_seq, public.df_purge_audit_id_seq
        TO laura_app;
        GRANT EXECUTE ON FUNCTION
          laura_private.due_df_orgs(integer),
          laura_private.purge_quarantine(uuid, timestamptz),
          laura_private.purge_record_versions(uuid, timestamptz),
          laura_private.resolve_quarantine(uuid, uuid, text, text) TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0012 is the data-foundation boundary; write a reviewed forward "
        "migration instead of dropping normalized customer data."
    )
