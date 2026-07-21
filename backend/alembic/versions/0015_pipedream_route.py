"""Allow 'pipedream' as a queued_actions.execution_route (Pipedream cutover).

The Pipedream Connect-Proxy executor stamps ``execution_route='pipedream'`` at
finalize. 0009's CHECK only permitted ``('', 'native', 'cedric', 'browser')``,
so persisting a Pipedream-routed action raised CheckViolation — which crashed
finalize and, because finalize also closes the meeting/session, stranded the
bot (meter-safety hazard). Widen the constraint to include 'pipedream'.

Idempotent (DROP … IF EXISTS then re-ADD). Already applied to prod out-of-band
via the admin role on 2026-07-21; this file keeps fresh/staging DBs correct.
"""
from alembic import op

revision = "0015_pipedream_route"
down_revision = "0014_browser_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE public.queued_actions
          DROP CONSTRAINT IF EXISTS queued_actions_execution_route_check;
        ALTER TABLE public.queued_actions
          ADD CONSTRAINT queued_actions_execution_route_check CHECK (
            execution_route IN ('', 'native', 'cedric', 'browser')
          );
        """
    )
