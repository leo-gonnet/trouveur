"""`job.ready_at`: when a posting first had everything the match reads.

Backfilled to `first_seen_at`, never to now(), which would make the whole corpus a new arrival on
the next run. A pending derive refill is ignored, so it does not hold back rows already usable.

Revision ID: 0025_job_ready
"""

from alembic import op

revision = "0025_job_ready"
down_revision = "0024_tenant_decided"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE job ADD COLUMN ready_at timestamptz")
    op.execute(
        "UPDATE job j SET ready_at = j.first_seen_at FROM job_facet f "
        "WHERE f.job_id = j.id AND f.derived_at IS NOT NULL AND NOT EXISTS ("
        "  SELECT 1 FROM work_item w WHERE w.job_id = j.id AND w.kind = 'detail' "
        "  AND w.attempts < 5)"
    )
    op.execute("CREATE INDEX job_ready_idx ON job (ready_at)")
    op.execute("CREATE INDEX job_not_ready_idx ON job (id) WHERE ready_at IS NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS job_not_ready_idx")
    op.execute("DROP INDEX IF EXISTS job_ready_idx")
    op.execute("ALTER TABLE job DROP COLUMN IF EXISTS ready_at")
