"""The rules cut is gone: retrieval feeds the reranker directly.

A deterministic reject between retrieval and scoring hid postings the user never saw and could
not find out about, and the value it protected -- a fraction of a cent per posting at the
reranker -- was already bounded by rerank_limit. Its verdict columns, the enum behind them and
the deal-breaker list it read are dropped rather than left to rot.

Revision ID: 0006_drop_rules_cut
"""

from alembic import op

revision = "0006_drop_rules_cut"
down_revision = "0005_drop_retrieval_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_job_match DROP COLUMN IF EXISTS rule_verdict")
    op.execute("ALTER TABLE user_job_match DROP COLUMN IF EXISTS rule_reason")
    op.execute("DROP TYPE IF EXISTS rule_verdict")
    op.execute("ALTER TABLE user_profile DROP COLUMN IF EXISTS deal_breakers")


def downgrade() -> None:
    op.execute("CREATE TYPE rule_verdict AS ENUM ('pass', 'reject', 'unknown')")
    op.execute(
        """
        ALTER TABLE user_job_match
            ADD COLUMN IF NOT EXISTS rule_verdict rule_verdict NOT NULL DEFAULT 'unknown',
            ADD COLUMN IF NOT EXISTS rule_reason text
        """
    )
    op.execute(
        """
        ALTER TABLE user_profile
            ADD COLUMN IF NOT EXISTS deal_breakers text[] NOT NULL DEFAULT '{}'
        """
    )
