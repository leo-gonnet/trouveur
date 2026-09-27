"""Retire the 384-wide vector space.

The encoder swap is complete: production reads and writes only `embedding_768`, and the 384-wide
vectors were already cleared. The column is dropped rather than left nullable, because a second
space nothing reads is a second place a reader could be pointed at by mistake.

The downgrade restores the column empty. Vectors are derived, so the old space is a refill away
and not something to keep a copy of.

Revision ID: 0019_drop_narrow_embedding
"""

from alembic import op

revision = "0019_drop_narrow_embedding"
down_revision = "0018_city_filter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE job_embedding DROP COLUMN IF EXISTS embedding")


def downgrade() -> None:
    op.execute("ALTER TABLE job_embedding ADD COLUMN IF NOT EXISTS embedding halfvec(384)")
