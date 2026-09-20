"""Room for a 768-wide vector space beside the 384-wide one.

Switching embedding models is a backfill measured in days on a four-core host, and the dense arm
has to keep working throughout. So the new space is a second column in the same row rather than a
second table: closing a posting deletes its row and therefore both vectors, which keeps the
"job_embedding holds open postings only" invariant in exactly one place instead of two.

The old column becomes nullable because, once the switch is complete, new postings are only ever
embedded into the new space. Nothing is dropped here -- retiring the 384 column is a separate
migration to run once nobody reads it.

Revision ID: 0008_wide_embedding
"""

from alembic import op

revision = "0008_wide_embedding"
down_revision = "0007_advert_expansion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE job_embedding ADD COLUMN IF NOT EXISTS embedding_768 halfvec(768)")
    op.execute("ALTER TABLE job_embedding ALTER COLUMN embedding DROP NOT NULL")
    # Partial, because during a backfill most rows have no wide vector yet and an index entry
    # for a NULL is dead weight in every walk.
    op.execute(
        "CREATE INDEX IF NOT EXISTS job_embedding_hnsw_768_idx ON job_embedding "
        "USING hnsw (embedding_768 halfvec_cosine_ops) WHERE embedding_768 IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS job_embedding_hnsw_768_idx")
    op.execute("ALTER TABLE job_embedding DROP COLUMN IF EXISTS embedding_768")
