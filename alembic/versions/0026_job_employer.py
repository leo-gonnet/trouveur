"""An index on the employer a posting belongs to, for the location filter.

The expression must stay character for character the one `db.queries.location.employer` writes,
or the planner cannot use it and every unplaced posting scans its employer's whole history.

Revision ID: 0026_job_employer
"""

from alembic import op

revision = "0026_job_employer"
down_revision = "0025_job_ready"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX job_employer_idx ON job "
        "((COALESCE(NULLIF(lower(company), ''), source || ':' || scope)))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS job_employer_idx")
