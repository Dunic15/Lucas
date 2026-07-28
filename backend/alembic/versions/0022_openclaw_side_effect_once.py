"""Enforce one OpenClaw side effect per canonical action.

Revision ID: 0022_openclaw_side_effect_once
Revises: 0021_openclaw_experiment
"""

from alembic import op


revision = "0022_openclaw_side_effect_once"
down_revision = "0021_openclaw_experiment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS openclaw_tool_calls_action_once
        ON public.openclaw_tool_calls(org_id, run_id, action_id)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS public.openclaw_tool_calls_action_once
        """
    )
