"""Retrieval depth is a constant, not a per-user setting.

Rerank takes the top rerank_limit by retrieval score whatever was fetched, so a per-user retrieval
limit changed nothing past the headroom the rules cut needs. It is now RETRIEVAL_LIMIT in
match/retrieve.py, and a column nothing reads is dropped rather than left to rot.

Revision ID: 0005_drop_retrieval_limit
"""

from alembic import op

revision = "0005_drop_retrieval_limit"
down_revision = "0004_match_only_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_profile DROP COLUMN IF EXISTS retrieval_limit")


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_profile
            ADD COLUMN IF NOT EXISTS retrieval_limit integer NOT NULL DEFAULT 400
        """
    )
