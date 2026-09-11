"""Drop the recommendation threshold.

The Recommendations page now shows everything that was reranked, ordered by score, so there is no
number to gate on. Volume is already decided by `rerank_limit`, which is the setting that costs
money; a second knob only let a user hide postings they had paid to have scored.

Revision ID: 0002_drop_notify_threshold
"""

from alembic import op

revision = "0002_drop_notify_threshold"
down_revision = "0001_pipeline_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_profile DROP COLUMN IF EXISTS notify_threshold")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE user_profile "
        "ADD COLUMN IF NOT EXISTS notify_threshold smallint NOT NULL DEFAULT 70"
    )
