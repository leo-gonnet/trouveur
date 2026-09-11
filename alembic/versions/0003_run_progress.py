"""Live run progress and cancellation.

The Scans page could say a run was `running` and nothing more: how far along it was, which source
it was on, and whether it was still moving were all invisible until it finished. The columns here
are the ones the runner can fill honestly while the work is in flight.

Revision ID: 0003_run_progress
"""

from alembic import op

revision = "0003_run_progress"
down_revision = "0002_drop_notify_threshold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A new enum member cannot be added inside a transaction that later uses it, but Alembic's
    # per-migration transaction never does, so this is safe here and avoids a table rewrite.
    op.execute("ALTER TYPE run_status ADD VALUE IF NOT EXISTS 'cancelled'")
    op.execute(
        """
        ALTER TABLE pipeline_run
            ADD COLUMN IF NOT EXISTS cancel_requested boolean NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS sources_total smallint,
            ADD COLUMN IF NOT EXISTS sources_done smallint NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS current_source text
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE pipeline_run
            DROP COLUMN IF EXISTS cancel_requested,
            DROP COLUMN IF EXISTS sources_total,
            DROP COLUMN IF EXISTS sources_done,
            DROP COLUMN IF EXISTS current_source
        """
    )
    # Postgres cannot drop an enum member; leaving 'cancelled' in place is harmless and the
    # alternative is recreating the type and rewriting the table.
