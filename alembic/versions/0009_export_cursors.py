"""Time-ordered indexes for the offsite corpus export.

The export walks the archive in arrival order -- `source_document` by `fetched_at`, `job` by
`first_seen_at`, closures by `closed_at` -- because that is the only order in which a day's
partition is a contiguous, immutable slice. None of those columns was indexed: until now nothing
read the corpus by when it arrived, only by what it was. Without these every nightly run is a
sequential scan of the largest tables in the database, and they only grow.

`closed_at` is partial because the overwhelming majority of rows are open and an index entry for
a NULL is dead weight in every walk.

Revision ID: 0009_export_cursors
"""

from alembic import op

revision = "0009_export_cursors"
down_revision = "0008_wide_embedding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS source_document_fetched_idx ON source_document (fetched_at)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS job_first_seen_idx ON job (first_seen_at)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS job_closed_idx ON job (closed_at) "
        "WHERE closed_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS job_closed_idx")
    op.execute("DROP INDEX IF EXISTS job_first_seen_idx")
    op.execute("DROP INDEX IF EXISTS source_document_fetched_idx")
