"""Add the non-destructive withdrawn Action state.

Revision ID: 0019_action_withdrawn
Revises: 0018_audit_log_writer
Create Date: 2026-07-23

Withdrawn actions remain tenant-scoped in queued_actions and their append-only
logs. They disappear from active work but remain visible in history/audit.
"""
from __future__ import annotations

from alembic import op

revision = "0019_action_withdrawn"
down_revision = "0018_audit_log_writer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.queued_actions
          DROP CONSTRAINT IF EXISTS queued_actions_execution_status_check;
        ALTER TABLE public.queued_actions
          ADD CONSTRAINT queued_actions_execution_status_check CHECK (
            execution_status IN (
              '', 'needs_details', 'proposed', 'approved', 'executing',
              'rejected', 'withdrawn', 'done', 'failed'
            )
          );
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "withdrawn is an auditable terminal state; roll forward instead"
    )
