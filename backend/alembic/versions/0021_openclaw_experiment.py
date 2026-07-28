"""OpenClaw full-executor experiment tables and route.

Revision ID: 0021_openclaw_experiment
Revises: 0020_callback_cancelled
"""
from __future__ import annotations

from alembic import op

revision = "0021_openclaw_experiment"
down_revision = "0020_callback_cancelled"
branch_labels = None
depends_on = None

_TABLES = (
    "openclaw_runs",
    "openclaw_action_runs",
    "openclaw_events",
    "openclaw_tool_calls",
)


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
        r"""
        ALTER TABLE public.queued_actions
          DROP CONSTRAINT IF EXISTS queued_actions_execution_route_check;
        ALTER TABLE public.queued_actions
          ADD CONSTRAINT queued_actions_execution_route_check CHECK (
            execution_route IN ('', 'native', 'cedric', 'browser', 'pipedream',
                                'manual', 'openclaw')
          );

        CREATE TABLE IF NOT EXISTS public.openclaw_runs (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          run_id text NOT NULL,
          meeting_id text NOT NULL,
          status text NOT NULL DEFAULT 'queued' CHECK (
            status IN ('queued', 'planning', 'running', 'needs_attention',
                       'done', 'failed', 'cancelled')
          ),
          gateway_run_id text NOT NULL DEFAULT '',
          input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          metrics_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          error text NOT NULL DEFAULT '',
          started_at timestamptz,
          finished_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, run_id),
          UNIQUE (org_id, meeting_id)
        );

        CREATE TABLE IF NOT EXISTS public.openclaw_action_runs (
          org_id uuid NOT NULL,
          run_id text NOT NULL,
          action_id text NOT NULL,
          status text NOT NULL DEFAULT 'queued' CHECK (
            status IN ('queued', 'planning', 'running', 'needs_attention',
                       'done', 'failed', 'cancelled')
          ),
          goal text NOT NULL DEFAULT '',
          result_summary text NOT NULL DEFAULT '',
          receipt_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          tools_json jsonb NOT NULL DEFAULT '[]'::jsonb,
          error text NOT NULL DEFAULT '',
          started_at timestamptz,
          finished_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, run_id, action_id),
          FOREIGN KEY (org_id, run_id)
            REFERENCES public.openclaw_runs(org_id, run_id)
            ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS public.openclaw_events (
          org_id uuid NOT NULL,
          id bigserial NOT NULL,
          run_id text NOT NULL,
          action_id text NOT NULL DEFAULT '',
          kind text NOT NULL,
          summary text NOT NULL DEFAULT '',
          tool text NOT NULL DEFAULT '',
          status text NOT NULL DEFAULT '',
          safe_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          FOREIGN KEY (org_id, run_id)
            REFERENCES public.openclaw_runs(org_id, run_id)
            ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS public.openclaw_tool_calls (
          org_id uuid NOT NULL,
          id bigserial NOT NULL,
          run_id text NOT NULL,
          action_id text NOT NULL,
          step_id text NOT NULL,
          idempotency_key text NOT NULL DEFAULT '',
          tool text NOT NULL,
          status text NOT NULL DEFAULT 'running' CHECK (
            status IN ('running', 'done', 'failed')
          ),
          request_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
          error text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id),
          UNIQUE (org_id, run_id, action_id, step_id),
          FOREIGN KEY (org_id, run_id)
            REFERENCES public.openclaw_runs(org_id, run_id)
            ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS openclaw_runs_org_created_idx
          ON public.openclaw_runs(org_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS openclaw_action_runs_run_idx
          ON public.openclaw_action_runs(org_id, run_id, created_at);
        CREATE INDEX IF NOT EXISTS openclaw_events_run_idx
          ON public.openclaw_events(org_id, run_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS openclaw_tool_calls_run_idx
          ON public.openclaw_tool_calls(org_id, run_id, created_at DESC);
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
        GRANT USAGE, SELECT ON SEQUENCE public.openclaw_events_id_seq TO laura_app;
        GRANT USAGE, SELECT ON SEQUENCE public.openclaw_tool_calls_id_seq TO laura_app;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE public.queued_actions
          DROP CONSTRAINT IF EXISTS queued_actions_execution_route_check;
        ALTER TABLE public.queued_actions
          ADD CONSTRAINT queued_actions_execution_route_check CHECK (
            execution_route IN ('', 'native', 'cedric', 'browser', 'pipedream',
                                'manual')
          );
        DROP TABLE IF EXISTS public.openclaw_tool_calls;
        DROP TABLE IF EXISTS public.openclaw_events;
        DROP TABLE IF EXISTS public.openclaw_action_runs;
        DROP TABLE IF EXISTS public.openclaw_runs;
        """
    )
