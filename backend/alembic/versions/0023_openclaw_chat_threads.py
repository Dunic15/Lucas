"""Persist org-scoped OpenClaw dashboard conversations.

Revision ID: 0023_openclaw_chat_threads
Revises: 0022_openclaw_side_effect_once
"""
from __future__ import annotations

from alembic import op


revision = "0023_openclaw_chat_threads"
down_revision = "0022_openclaw_side_effect_once"
branch_labels = None
depends_on = None

_TABLES = ("openclaw_chat_threads", "openclaw_chat_messages")


def _tenant_policy(table: str) -> None:
    op.execute(
        f"""
        ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation ON public.{table};
        CREATE POLICY tenant_isolation ON public.{table}
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


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.openclaw_chat_threads (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          thread_id text NOT NULL,
          title text NOT NULL DEFAULT 'New chat',
          meeting_id text NOT NULL DEFAULT '',
          archived boolean NOT NULL DEFAULT false,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, thread_id)
        );

        CREATE TABLE IF NOT EXISTS public.openclaw_chat_messages (
          org_id uuid NOT NULL,
          id bigserial NOT NULL,
          thread_id text NOT NULL,
          role text NOT NULL CHECK (role IN ('user', 'assistant')),
          body text NOT NULL DEFAULT '',
          workflow_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          FOREIGN KEY (org_id, thread_id)
            REFERENCES public.openclaw_chat_threads(org_id, thread_id)
            ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS openclaw_chat_threads_recent_idx
          ON public.openclaw_chat_threads(org_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS openclaw_chat_messages_thread_idx
          ON public.openclaw_chat_messages(org_id, thread_id, created_at, id);
        """
    )
    for table in _TABLES:
        _tenant_policy(table)
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC;")
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{table} TO laura_app;"
        )
    op.execute(
        """
        GRANT USAGE, SELECT ON SEQUENCE
          public.openclaw_chat_messages_id_seq TO laura_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS public.openclaw_chat_messages;
        DROP TABLE IF EXISTS public.openclaw_chat_threads;
        """
    )
