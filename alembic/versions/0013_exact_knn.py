"""Drop the ANN indexes: the dense arm now searches exactly.

An HNSW index applies a query's WHERE clause *after* the graph walk, so a filtered search returns
fewer rows than it was asked for and reports nothing about having done so. The narrower the filter,
the worse it gets: searching for postings in one small city, the walk spends its whole working set
on postings elsewhere and the join then discards them. `hnsw.ef_search` and over-fetching only
widen that window, they do not close it, and neither can be tuned against a filter whose
selectivity depends on whose profile is running.

Without an index there is no walk, so the filter is applied first by construction and the result is
the true nearest neighbours. Measured on the production join shape (job_embedding + job +
job_facet, 7-day horizon) at 250,000 vectors: 14ms when the filter qualifies 565 rows, 68ms at
38,315, 80ms unfiltered. At sixteen dense queries a run, that is under three seconds for a job
that runs once a day -- against a corpus that holds one week of postings and sits in the tens of
thousands.

The cost is linear in vectors inside the horizon, so this is a decision about corpus size and not
a permanent one. Reinstate an index only if that count reaches several million.

Revision ID: 0013_exact_knn
"""

from alembic import op

revision = "0013_exact_knn"
down_revision = "0012_immutable_editions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS job_embedding_hnsw_idx")
    op.execute("DROP INDEX IF EXISTS job_embedding_hnsw_768_idx")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS job_embedding_hnsw_idx ON job_embedding "
        "USING hnsw (embedding halfvec_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS job_embedding_hnsw_768_idx ON job_embedding "
        "USING hnsw (embedding_768 halfvec_cosine_ops) WHERE embedding_768 IS NOT NULL"
    )
