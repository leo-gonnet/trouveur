"""A run that matches one user and sweeps nothing.

A profile change forgets the user's verdicts, and until the next scheduled scan the page is
empty. Waiting half an hour for Arbeitsagentur to be swept again in order to re-rank postings we
already hold is the wrong trade, so a run may name a user and skip ingest entirely. It goes
through the same queue so it is visible, cancellable while queued and recorded like any other.

Revision ID: 0004_match_only_run
"""

from alembic import op

revision = "0004_match_only_run"
down_revision = "0003_run_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE pipeline_run
            ADD COLUMN IF NOT EXISTS match_user_id bigint REFERENCES app_user(id) ON DELETE CASCADE
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE pipeline_run DROP COLUMN IF EXISTS match_user_id")
