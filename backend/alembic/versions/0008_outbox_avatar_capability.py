"""Per-avatar Slack capability gate on the callback outbox.

Revision ID: 0008_outbox_avatar_capability
Revises: 0007_durable_artifacts
Create Date: 2026-07-15

Mirrors the SQLite change in app/outbox.py: the durable callback_outbox row now
carries the acting avatar_id (plain text, no ::uuid — org_id remains the sole
RLS/tenant key). process_due reads it to honour the per-avatar `slack` toggle
(#221) on the Cedric broker path, retiring a forbidden row as a terminal
`skipped_capability` status instead of delivering it. Legacy rows have a NULL
avatar_id and deliver unchanged.
"""
from __future__ import annotations

from alembic import op

revision = "0008_outbox_avatar_capability"
down_revision = "0007_durable_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        -- Nullable: existing rows stay NULL (unknown avatar) and process_due
        -- delivers them exactly as before. Only an EXPLICIT slack=False on a
        -- stamped avatar suppresses delivery. No FK, no ::uuid: avatar_id is a
        -- free string keyed like avatar_capabilities (split-brain safe).
        ALTER TABLE public.callback_outbox
          ADD COLUMN IF NOT EXISTS avatar_id text;

        -- Add the terminal skip state to the existing status domain. The
        -- discovery function and claim query only consider pending/failed/
        -- sending, so a skipped_capability row is never re-sent.
        ALTER TABLE public.callback_outbox
          DROP CONSTRAINT IF EXISTS callback_outbox_status_check;
        ALTER TABLE public.callback_outbox
          ADD CONSTRAINT callback_outbox_status_check
          CHECK (
            status IN (
              'pending', 'sending', 'failed', 'delivered', 'skipped_capability'
            )
          );
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0008 gates Slack delivery per avatar; roll forward with a reviewed "
        "migration instead of returning ungated callbacks to production."
    )
