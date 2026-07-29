"""Microsoft Graph as Data Foundation connector #1.

Revision ID: 0024_graph_connector
Revises: 0023_openclaw_chat_threads
Create Date: 2026-07-29

Additive-only: the Data Foundation schema (0012) already models everything a
Graph sync needs — connectors, stable record heads + immutable versions,
incremental cursors, quarantine, mirrored identities/groups/edges, and
per-record ACL entries. The ONLY schema change Graph requires is admitting a
new connector ``kind``: extend the ``df_connectors.kind`` CHECK to include
``'graph'`` (M365 / SharePoint / OneDrive documents + Entra ID identities).

No new tables, no new grants: a Graph connector reuses the exact FORCE-RLS
tenant policy, least-grant table privileges, and SECURITY DEFINER discovery
functions migration 0012 installed. Rollback is flag-off
(GRAPH_CONNECTOR_ENABLED / DATA_FOUNDATION_ENABLED); like every DF/knowledge
boundary migration, downgrade raises rather than silently narrowing a CHECK
that customer rows may already satisfy.
"""
from __future__ import annotations

from alembic import op

revision = "0024_graph_connector"
down_revision = "0023_openclaw_chat_threads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Re-point the CHECK to the extended set. DROP+ADD is the only way to widen
    # a CHECK constraint in Postgres; the ADD is validated against existing
    # rows, all of which use the pre-existing kinds, so it can never fail on a
    # populated table.
    op.execute(
        r"""
        ALTER TABLE public.df_connectors
          DROP CONSTRAINT IF EXISTS df_connectors_kind_check;
        ALTER TABLE public.df_connectors
          ADD CONSTRAINT df_connectors_kind_check
          CHECK (kind IN ('upload', 'gdrive', 'graph', 'slack', 'notion',
                          'crm', 'custom'));
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0024 widens the DF connector-kind boundary; narrowing it could "
        "orphan live Graph connectors — write a reviewed forward migration."
    )
