"""Allow 'manual' as a queued_actions.execution_route (track-only cards).

#357 ("Cedric lives inside Slack") stamps ``execution_route='manual'`` for
unexecutable captures in orgs without a linked Slack agent. The CHECK from
0015 didn't include it, so the live-capture INSERT raised CheckViolation —
which 500'd the recall webhook (the avatar fell silent instead of confirming
the ask) and crashed finalize, stranding the session exactly like the
incident 0015's own docstring warns about (2026-07-22, meeting qwp-hzjw-bxm:
"can you send an email" got no confirmation and the meeting couldn't be
ended from voice OR dashboard until the constraint was widened out-of-band).

Lesson, twice-learned and now twice-written: A NEW execution_route VALUE
SHIPS WITH ITS MIGRATION IN THE SAME PR.

Idempotent (DROP … IF EXISTS then re-ADD). Already applied to prod
out-of-band via the admin role on 2026-07-22; this file keeps fresh/staging
DBs correct.
"""
from alembic import op

revision = "0016_manual_route"
down_revision = "0015_pipedream_route"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE public.queued_actions
          DROP CONSTRAINT IF EXISTS queued_actions_execution_route_check;
        ALTER TABLE public.queued_actions
          ADD CONSTRAINT queued_actions_execution_route_check CHECK (
            execution_route IN ('', 'native', 'cedric', 'browser', 'pipedream',
                                'manual')
          );
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE public.queued_actions
          DROP CONSTRAINT IF EXISTS queued_actions_execution_route_check;
        ALTER TABLE public.queued_actions
          ADD CONSTRAINT queued_actions_execution_route_check CHECK (
            execution_route IN ('', 'native', 'cedric', 'browser', 'pipedream')
          );
        """
    )
