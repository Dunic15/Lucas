"""org_id spine: identity tables + org_id NOT NULL on every persisted table + RLS.

Revision ID: 0001_org_id_spine
Revises:
Create Date: 2026-07-13

The irreversible minimum (MULTI-TENANCY.md §4, MULTI-TENANCY-IMPLEMENTATION.md
§3), written for Supabase Postgres, eu-central-1. The app connects as a
dedicated NON-superuser role ``laura_app`` (superusers bypass RLS).

This migration is NOT exercised by the SQLite test suite; the store/ledger
bootstrap mirrors the same org_id columns in SQLite so ``WHERE org_id=?`` is
tested locally, and Alembic env.py is a no-op when LAURA_DATABASE_URL is empty.
It runs only against the future Postgres control plane.

Ordering is obligatory (§3): identity spine → seed Demo org (so backfills'
foreign keys hold) → per table ADD COLUMN org_id (nullable) → UPDATE →
SET NOT NULL → keys/indexes → RLS ENABLE/FORCE + policies.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.config import settings  # noqa: E402

# revision identifiers, used by Alembic.
revision = "0001_org_id_spine"
down_revision = None
branch_labels = None
depends_on = None

DEMO_ORG_ID = settings.demo_org_id

# The six persisted tables that gain a NOT NULL org_id. Backfill: sessions/users
# from '' → DEMO; the rest JOIN sessions on bot_id when possible, else DEMO.
_ORG_SCOPED_TABLES = (
    "sessions",
    "conversation_routes",
    "utterances",
    "artifacts",
    "scheduled_events",
    "ledger_items",
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")   # gen_random_uuid()
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")     # case-insensitive email

    # ── 1. identity spine (empty now; SSO/SCIM attach later) ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS orgs (
          id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          name              text NOT NULL,
          slug              text UNIQUE NOT NULL,
          plan              text NOT NULL DEFAULT 'free',
          region            text NOT NULL DEFAULT 'eu-central-1',
          sso_connection_id text,
          auth_method       text NOT NULL DEFAULT 'token',
          retention_days    int  NOT NULL DEFAULT 90,
          created_at        timestamptz NOT NULL DEFAULT now(),
          deleted_at        timestamptz
        );
        CREATE TABLE IF NOT EXISTS users (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          email citext UNIQUE NOT NULL, name text,
          external_idp_subject text, provider text,
          auth_method text NOT NULL DEFAULT 'token',
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS memberships (
          user_id uuid NOT NULL REFERENCES users(id),
          org_id  uuid NOT NULL REFERENCES orgs(id),
          role text NOT NULL DEFAULT 'member',     -- owner|admin|member|billing
          status text NOT NULL DEFAULT 'active',
          PRIMARY KEY (user_id, org_id)
        );
        CREATE TABLE IF NOT EXISTS org_domains (
          org_id uuid NOT NULL REFERENCES orgs(id),
          domain text NOT NULL,                    -- NEVER map gmail.com etc.
          verified_at timestamptz,
          PRIMARY KEY (org_id, domain)
        );
        CREATE TABLE IF NOT EXISTS org_agents (
          org_id uuid NOT NULL REFERENCES orgs(id),
          avatar_id text NOT NULL,
          alias text NOT NULL DEFAULT '',
          visibility text NOT NULL DEFAULT 'org',
          status text NOT NULL DEFAULT 'active',
          PRIMARY KEY (org_id, avatar_id)
        );
        CREATE TABLE IF NOT EXISTS org_tokens (
          token_hash text PRIMARY KEY,             -- replaces the global bearer
          org_id uuid NOT NULL REFERENCES orgs(id),
          label text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    # ── 2. seed the Demo org BEFORE any backfill (so FKs hold) ──
    op.execute(
        f"""
        INSERT INTO orgs (id, name, slug, plan)
        VALUES ('{DEMO_ORG_ID}', 'Demo', 'demo', 'demo')
        ON CONFLICT (id) DO NOTHING;
        """
    )

    # ── 3. the persisted tables in their PRE-tenancy shape (create if the
    #        control plane is fresh; a no-op if they already carry data) ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
          bot_id text PRIMARY KEY,
          meeting_url text NOT NULL, avatar_id text NOT NULL DEFAULT 'laura',
          anam_conversation_id text NOT NULL DEFAULT '',
          anam_conversation_url text NOT NULL DEFAULT '',
          last_spoke_at double precision NOT NULL DEFAULT 0,
          proactive_done boolean NOT NULL DEFAULT false,
          integration jsonb,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS conversation_routes (
          conversation_id text PRIMARY KEY, bot_id text NOT NULL
        );
        CREATE TABLE IF NOT EXISTS utterances (
          bot_id text NOT NULL, speaker text NOT NULL, text text NOT NULL,
          ts double precision NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
          bot_id text PRIMARY KEY, artifact jsonb NOT NULL,
          visibility text NOT NULL DEFAULT 'participants',
          saved_at timestamptz NOT NULL DEFAULT now(),
          delete_by timestamptz
        );
        CREATE TABLE IF NOT EXISTS scheduled_events (
          event_id text PRIMARY KEY, marked_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS ledger_items (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          meeting_key text NOT NULL, avatar_id text NOT NULL, kind text NOT NULL,
          item text NOT NULL, item_norm text NOT NULL DEFAULT '',
          owner text NOT NULL DEFAULT '', deadline text NOT NULL DEFAULT '',
          meeting_type text NOT NULL DEFAULT '', status text NOT NULL DEFAULT 'open',
          bot_id text NOT NULL DEFAULT '', action_id text NOT NULL DEFAULT '',
          created_at double precision NOT NULL DEFAULT 0,
          resolved_at double precision, resolved_by_bot_id text NOT NULL DEFAULT '',
          resolution_detail text NOT NULL DEFAULT ''
        );
        """
    )
    # item_norm backfill for any legacy ledger rows (regexp_replace mirrors
    # _norm: collapse non-word runs to a single space, lower, trim).
    op.execute(
        "UPDATE ledger_items "
        "SET item_norm = trim(regexp_replace(lower(item), '\\W+', ' ', 'g')) "
        "WHERE item_norm = ''"
    )

    # ── 4. ADD COLUMN org_id (nullable) → backfill DEMO → SET NOT NULL ──
    for table in _ORG_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS org_id uuid")
    # utterances/conversation_routes/artifacts inherit their org from the owning
    # session where one exists; everything else (and orphans) → the Demo org.
    op.execute(
        f"""
        UPDATE sessions             SET org_id = '{DEMO_ORG_ID}' WHERE org_id IS NULL;
        UPDATE scheduled_events     SET org_id = '{DEMO_ORG_ID}' WHERE org_id IS NULL;
        UPDATE ledger_items         SET org_id = '{DEMO_ORG_ID}' WHERE org_id IS NULL;
        UPDATE utterances u SET org_id = COALESCE(
            (SELECT s.org_id FROM sessions s WHERE s.bot_id = u.bot_id), '{DEMO_ORG_ID}')
          WHERE u.org_id IS NULL;
        UPDATE conversation_routes c SET org_id = COALESCE(
            (SELECT s.org_id FROM sessions s WHERE s.bot_id = c.bot_id), '{DEMO_ORG_ID}')
          WHERE c.org_id IS NULL;
        UPDATE artifacts a SET org_id = COALESCE(
            (SELECT s.org_id FROM sessions s WHERE s.bot_id = a.bot_id), '{DEMO_ORG_ID}')
          WHERE a.org_id IS NULL;
        """
    )
    for table in _ORG_SCOPED_TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN org_id SET NOT NULL, "
            f"ADD CONSTRAINT fk_{table}_org FOREIGN KEY (org_id) REFERENCES orgs(id)"
        )

    # ── 5. keys/indexes lead with org_id (the composite tenancy keys) ──
    op.execute(
        """
        -- sessions PK (org_id, bot_id); bot_id stays globally unique (Recall id).
        ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_pkey;
        ALTER TABLE sessions ADD PRIMARY KEY (org_id, bot_id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_bot ON sessions(bot_id);

        -- conversation_routes PK (org_id, conversation_id) + a GLOBAL unique on
        -- conversation_id: the unauthenticated ws/{conversation_id} must resolve
        -- its org from the conversation_id alone before any read.
        ALTER TABLE conversation_routes DROP CONSTRAINT IF EXISTS conversation_routes_pkey;
        ALTER TABLE conversation_routes ADD PRIMARY KEY (org_id, conversation_id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_conv_global
            ON conversation_routes(conversation_id);

        CREATE INDEX IF NOT EXISTS idx_utt_org_bot ON utterances(org_id, bot_id);

        ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_pkey;
        ALTER TABLE artifacts ADD PRIMARY KEY (org_id, bot_id);
        CREATE INDEX IF NOT EXISTS idx_artifacts_org_saved
            ON artifacts(org_id, saved_at);

        -- ledger dedupe + hot lookups become org-scoped.
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_dedupe
            ON ledger_items(org_id, meeting_key, kind, item_norm);
        CREATE INDEX IF NOT EXISTS idx_ledger_org_key_status
            ON ledger_items(org_id, meeting_key, status);
        CREATE INDEX IF NOT EXISTS idx_ledger_org_action
            ON ledger_items(org_id, action_id);
        """
    )

    # ── 6. governance: append-only audit_log (metadata only, never transcript) ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
          org_id uuid NOT NULL REFERENCES orgs(id),
          actor_user_id uuid, action text NOT NULL, target text,
          ts timestamptz NOT NULL DEFAULT now()
        );
        REVOKE UPDATE, DELETE ON audit_log FROM laura_app;
        """
    )

    # ── 7. RLS: isolation becomes a DB invariant (ENABLE + FORCE so even the
    #        table owner can't bypass), policy keyed on the per-txn GUC. ──
    _rls_tables = _ORG_SCOPED_TABLES + (
        "orgs", "memberships", "org_domains", "org_agents", "org_tokens", "audit_log",
    )
    for table in _rls_tables:
        col = "id" if table == "orgs" else "org_id"
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE  ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS tenant_isolation ON {table};
            CREATE POLICY tenant_isolation ON {table}
              USING      ({col} = current_setting('app.current_org', true)::uuid)
              WITH CHECK ({col} = current_setting('app.current_org', true)::uuid);
            """
        )


def downgrade() -> None:
    # Irreversible-minimum migration: dropping org_id would re-introduce the
    # cross-tenant leak this exists to prevent. Intentionally not reversible.
    raise NotImplementedError(
        "0001 is the org_id spine; downgrading would drop tenant isolation."
    )
