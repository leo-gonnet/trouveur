"""The step a run is in, so a run past its sweep does not look finished or stuck.

Revision ID: 0029_run_stage
"""

from alembic import op

revision = "0029_run_stage"
down_revision = "0028_linkedin_leads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pipeline_run ADD COLUMN stage text")


def downgrade() -> None:
    op.execute("ALTER TABLE pipeline_run DROP COLUMN IF EXISTS stage")
