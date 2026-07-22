"""First-class Decision records — decision_maker, reason, supersedes linking.

Revision ID: 0017_decisions
Revises: 0016_manual_route
Create Date: 2026-07-22

Decisions used to live only as ``artifact['decisions']`` — a ``list[str]`` of
one-liners with no identity, no author, and no way to say "this decision
supersedes the one from July 15". This migration gives a decision its own
durable, tenant-scoped record so the product can show a decision's maker, its
reason, and its supersede chain — the defensible differentiator.

Table ``public.meeting_decisions`` (NOT ``action_decisions`` — that name is
TAKEN by 0009 for the approve/reject verdict ledger). One row per decision the
post-meeting extractor emits, bound to (org_id, id):

  - ``decision``        the decision text (one short line).
  - ``decision_maker``  who made it (backfilled from the tracker's speaker).
  - ``reason``          why (the rationale, when stated).
  - ``related_project`` the project/workstream it concerns (supersede scope).
  - ``supersedes``      the id of an EARLIER decision this one overrides.
  - ``status``          active | superseded | revisited — never two active on
                        the same supersede link.
  - ``source_ref``      a bot_id / meeting_key ONLY — NEVER transcript text
                        (transcripts are PII and never land in the control
                        plane).

Discipline mirrors 0009-0016: composite-org PK, org FK ON DELETE CASCADE,
FORCE ROW LEVEL SECURITY under the ``app.current_org`` policy, REVOKE-from-
everyone then GRANT SELECT/INSERT/UPDATE (no DELETE — decisions are
superseded/revisited, never deleted by the runtime) to exactly ``laura_app``.
Downgrade raises: a forward migration retires this table, never a drop that
loses decision provenance.
"""
from __future__ import annotations

from alembic import op

revision = "0017_decisions"
down_revision = "0016_manual_route"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE public.meeting_decisions (
          org_id uuid NOT NULL REFERENCES public.orgs(id) ON DELETE CASCADE,
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          bot_id text,
          decision text NOT NULL,
          decision_maker text,
          reason text,
          related_project text,
          supersedes uuid,
          status text NOT NULL DEFAULT 'active' CHECK (
            status IN ('active', 'superseded', 'revisited')
          ),
          -- A bot_id / meeting_key ONLY — never transcript text (PII).
          source_ref text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (org_id, id)
        );
        CREATE INDEX idx_meeting_decisions_bot
          ON public.meeting_decisions(org_id, bot_id)
          WHERE bot_id IS NOT NULL;
        CREATE INDEX idx_meeting_decisions_project
          ON public.meeting_decisions(org_id, related_project)
          WHERE related_project IS NOT NULL;
        """
    )

    op.execute(
        """
        ALTER TABLE public.meeting_decisions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.meeting_decisions FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON public.meeting_decisions
          TO laura_app
          USING (
            org_id = NULLIF(current_setting('app.current_org', true), '')::uuid
          )
          WITH CHECK (
            org_id = NULLIF(current_setting('app.current_org', true), '')::uuid
          );
        """
    )

    op.execute(
        r"""
        REVOKE ALL ON TABLE public.meeting_decisions FROM PUBLIC;

        DO $roles$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'anon'
          ) THEN
            REVOKE ALL ON TABLE public.meeting_decisions FROM anon;
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticated'
          ) THEN
            REVOKE ALL ON TABLE public.meeting_decisions FROM authenticated;
          END IF;
        END
        $roles$;

        REVOKE ALL ON TABLE public.meeting_decisions FROM laura_app;
        GRANT SELECT, INSERT, UPDATE
          ON TABLE public.meeting_decisions TO laura_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0017 is the first-class decision record boundary; write a reviewed "
        "forward migration instead of dropping decision provenance."
    )
