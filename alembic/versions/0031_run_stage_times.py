"""When each step of a run began, so Operations can show where a run's time went.

Revision ID: 0031_run_stage_times
"""

from alembic import op

revision = "0031_run_stage_times"
down_revision = "0030_schedule_in_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pipeline_run ADD COLUMN stage_started jsonb NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    op.execute("ALTER TABLE pipeline_run DROP COLUMN stage_started")
