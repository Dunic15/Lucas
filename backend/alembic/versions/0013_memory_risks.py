"""Meeting Memory: persist the artifact's risks.

Revision ID: 0013_memory_risks
Revises: 0012_meeting_memory
Create Date: 2026-07-31

The post-meeting artifact has carried ``risks[]`` (distilled one-liners, never
transcript) since day one, but ``meeting_memory.deposit`` dropped them — risks
were spoken in meetings, extracted, shipped to the outbox… and forgotten by
the week brief. One additive column closes the loop; the digest and Laura's
status report read it. Backward-safe: existing rows default to ``'[]'``.
"""
from __future__ import annotations

from alembic import op

revision = "0013_memory_risks"
down_revision = "0012_meeting_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.memory_meetings "
        "ADD COLUMN risks_json text NOT NULL DEFAULT '[]'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.memory_meetings DROP COLUMN risks_json")
