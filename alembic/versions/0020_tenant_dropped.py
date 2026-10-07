"""A board can be tried and dropped, rather than only registered or forgotten.

`tenants remove` deleted the row, so a board we had probed and decided against left no trace and
the next discovery pass proposed it again. `dropped_at` records the decision: the row stays,
disabled, with the reason in `note`, and the coverage report can say how many boards were tried
and rejected rather than never tried at all.

Nullable rather than a state column, because `enabled` and `origin` already carry the other two
answers and a third enum would make three columns that can disagree.

Revision ID: 0020_tenant_dropped
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_tenant_dropped"
down_revision = "0019_drop_narrow_embedding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source_tenant", sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("source_tenant", "dropped_at")
