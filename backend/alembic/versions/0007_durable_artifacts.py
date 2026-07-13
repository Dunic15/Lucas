"""Durable, tenant-scoped meeting artifacts for the customer archive.

Revision ID: 0007_durable_artifacts
Revises: 0006_postgres_outbox
"""
from __future__ import annotations

from alembic import op

revision = "0007_durable_artifacts"
down_revision = "0006_postgres_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 already owns the composite (org_id, bot_id) primary key and FORCE
    # RLS policy. 0004 deliberately revoked runtime table access. Restore only
    # the operations the tenant-scoped artifact DAL needs; there is no DELETE
    # grant until a reviewed retention worker exists.
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE
          ON TABLE public.artifacts TO laura_app;

        -- Artifact delete_by is stamped from the existing per-org retention
        -- contract. RLS on orgs exposes only app.current_org, and only these
        -- two columns are visible to the runtime role.
        GRANT SELECT (id, retention_days)
          ON TABLE public.orgs TO laura_app;
        """
    )


def downgrade() -> None:
    # Revoking this grant while the production code still relies on Postgres
    # would turn every finalization into a retry. Roll forward with a reviewed
    # replacement instead.
    raise NotImplementedError(
        "0007 is the durable artifact boundary; replace it forward"
    )
