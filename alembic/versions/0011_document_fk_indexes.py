"""Index the two job columns that point back at a document.

`job.listing_document_id` and `job.detail_document_id` both carry `ON DELETE SET NULL` back to
`source_document`, and neither was indexed. Postgres does not index the referencing side of a
foreign key for you, so every row deleted from `source_document` had to find its referents by
sequential scan of `job` -- twice, once per constraint.

Retiring a source is the operation that turns that into an outage. Deleting Arbeitsagentur's
351,353 documents against a 141,142-row `job` table ran for over an hour without finishing and was
cancelled; with these two indexes the same delete took 29 seconds. The cost is two b-trees on a
column that is NULL for most rows and a few bytes per insert, which is nothing beside a table scan
per deleted document.

Written with IF NOT EXISTS because the indexes were created by hand on the deployment that hit
this, and a migration that fails on the one database it was written for is not much of a fix.

Revision ID: 0011_document_fk_indexes
"""

from alembic import op

revision = "0011_document_fk_indexes"
down_revision = "0010_profile_background"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS job_listing_document_idx ON job (listing_document_id)")
    op.execute("CREATE INDEX IF NOT EXISTS job_detail_document_idx ON job (detail_document_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS job_detail_document_idx")
    op.execute("DROP INDEX IF EXISTS job_listing_document_idx")
