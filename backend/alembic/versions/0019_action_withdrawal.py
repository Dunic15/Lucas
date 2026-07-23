"""Add withdrawn to the canonical action lifecycle.

Revision ID: 0019_action_withdrawal
Revises: 0018_audit_log_writer
Create Date: 2026-07-23

Withdrawal is a terminal, auditable state. Actions are never deleted: proposed,
needs-details, or safely idle approved rows transition to withdrawn and remain
visible in history. Executing and settled work remains immutable.
"""
from __future__ import annotations

from alembic import op

revision = "0019_action_withdrawal"
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
        "Withdrawn actions are preserved history; use a reviewed forward "
        "migration instead of erasing their lifecycle state."
    )
