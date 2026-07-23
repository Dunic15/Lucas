"""Enable the append-only runtime security audit writer.

Revision ID: 0018_audit_log_writer
Revises: 0017_decisions
Create Date: 2026-07-23

The table and FORCE RLS policy have existed since 0001, but migration 0004
revoked every runtime privilege and never granted INSERT back. This migration
makes the audit table usable without weakening its append-only contract.

Rows contain metadata only: tenant id, optional human actor id, action name,
opaque target id, and timestamp. Retention is 180 days; deletion belongs to an
operator-owned scheduled maintenance job, never the application role.
"""
from __future__ import annotations

from alembic import op

revision = "0018_audit_log_writer"
down_revision = "0017_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        REVOKE ALL ON TABLE public.audit_log FROM PUBLIC;
        REVOKE ALL ON TABLE public.audit_log FROM laura_app;
        GRANT INSERT ON TABLE public.audit_log TO laura_app;
        REVOKE SELECT, UPDATE, DELETE, TRUNCATE
          ON TABLE public.audit_log FROM laura_app;

        CREATE INDEX IF NOT EXISTS idx_audit_log_org_ts
          ON public.audit_log (org_id, ts DESC);
        """
    )


def downgrade() -> None:
    # Removing the writer would silently erase security observability. Replace
    # this migration with a reviewed forward migration instead.
    raise NotImplementedError(
        "0018 enables append-only security auditing; roll forward instead"
    )
