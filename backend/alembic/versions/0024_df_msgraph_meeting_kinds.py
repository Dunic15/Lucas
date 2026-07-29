"""Company Brain M2/M3: admit the msgraph and meeting connector kinds.

Revision ID: 0024_df_msgraph_meeting_kinds
Revises: 0023_openclaw_chat_threads
Create Date: 2026-07-29

The Data Foundation (0012) fixed its connector vocabulary in a CHECK
constraint. Two new first-party sources need admission:

- ``msgraph`` — Microsoft Graph (SharePoint/OneDrive/Entra), the first
  external production connector. Mirrors source ACLs, so its records are
  ``acl_mode='mirrored'`` and reach a caller only through the ContextResolver.
- ``meeting`` — native Laura meetings (Meeting Memory). A finalized meeting
  artifact emits ONE distilled envelope: summary, decisions, actions, risks,
  open questions. Never the transcript (PII stays in the artifact store).

Additive only: the constraint is widened to a strict superset, so every
existing row stays valid and no data moves. Nothing here touches the ACL
model, the retrieval boundary, or the action plane — the new kinds inherit
DF's fail-closed visibility exactly as gdrive does.
"""
from __future__ import annotations

from alembic import op

revision = "0024_df_msgraph_meeting_kinds"
down_revision = "0023_openclaw_chat_threads"
branch_labels = None
depends_on = None

_KINDS = (
    "upload", "gdrive", "slack", "notion", "crm", "custom",
    "msgraph", "meeting",
)


def upgrade() -> None:
    kinds = ", ".join(f"'{k}'" for k in _KINDS)
    op.execute(
        f"""
        ALTER TABLE public.df_connectors
          DROP CONSTRAINT IF EXISTS df_connectors_kind_check;
        ALTER TABLE public.df_connectors
          ADD CONSTRAINT df_connectors_kind_check
          CHECK (kind IN ({kinds}));
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0024 only widens the DF connector vocabulary; narrowing it again "
        "would orphan live msgraph/meeting connectors. Write a reviewed "
        "forward migration instead."
    )
