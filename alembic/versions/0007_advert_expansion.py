"""Store the generated adverts beside the generated queries.

Expansion used to be one list of short phrases that both retrieval arms ran. It is now two
kinds of thing with different destinations: phrases, which both arms can use, and synthetic
adverts, which only the dense arm can -- an advert through websearch_to_tsquery becomes a
hundred ANDed terms and matches nothing. A second column rather than a JSONB blob because the
shape is fixed and text[] is what the reader already expects.

Revision ID: 0007_advert_expansion
"""

from alembic import op

revision = "0007_advert_expansion"
down_revision = "0006_drop_rules_cut"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_query_expansion
            ADD COLUMN IF NOT EXISTS adverts text[] NOT NULL DEFAULT '{}'
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE user_query_expansion DROP COLUMN IF EXISTS adverts")
