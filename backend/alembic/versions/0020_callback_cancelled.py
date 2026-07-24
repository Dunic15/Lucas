"""callback_outbox: allow status='cancelled'.

The withdrawal machinery (0019-era code paths: dashboard withdraw + voice
withdraw) sets callback_outbox.status='cancelled' on the action's undelivered
callbacks, but the 0006 check constraint only allowed
pending/sending/failed/delivered — every withdraw of an orchestrated action
500'd with CheckViolation in production (live 2026-07-24,
POST /dashboard/actions/{id}/withdraw). The pg test lane auto-skips without
pgserver, which is how this shipped unseen. Pure constraint widening: no data
change, instant, and the delivery worker only picks pending/failed so
'cancelled' rows stay terminal.

Revision ID: 0020_callback_cancelled
Revises: 0019_action_withdrawal
"""
from alembic import op

revision = "0020_callback_cancelled"
down_revision = "0019_action_withdrawal"
branch_labels = None
depends_on = None

_ALLOWED = "('pending', 'sending', 'failed', 'delivered', 'cancelled')"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.callback_outbox
          DROP CONSTRAINT IF EXISTS callback_outbox_status_check;
        """
    )
    op.execute(
        f"""
        ALTER TABLE public.callback_outbox
          ADD CONSTRAINT callback_outbox_status_check
          CHECK (status IN {_ALLOWED});
        """
    )


def downgrade() -> None:
    # Narrowing back requires no 'cancelled' rows; map them to a terminal
    # status first so the constraint can re-apply.
    op.execute(
        """
        UPDATE public.callback_outbox
           SET status='delivered', last_error='was: cancelled'
         WHERE status='cancelled';
        """
    )
    op.execute(
        """
        ALTER TABLE public.callback_outbox
          DROP CONSTRAINT IF EXISTS callback_outbox_status_check;
        """
    )
    op.execute(
        """
        ALTER TABLE public.callback_outbox
          ADD CONSTRAINT callback_outbox_status_check
          CHECK (status IN ('pending', 'sending', 'failed', 'delivered'));
        """
    )
