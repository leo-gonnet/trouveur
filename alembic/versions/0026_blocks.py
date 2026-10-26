"""A sweep the provider refused, recorded apart from a sweep that found nothing.

Revision ID: 0026_blocks
"""

from alembic import op

revision = "0026_blocks"
down_revision = "0025_job_ready"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE source_sweep ADD COLUMN blocked boolean NOT NULL DEFAULT false")
    op.execute(
        "ALTER TABLE source_scope_health ADD COLUMN blocked boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE source_scope_health DROP COLUMN IF EXISTS blocked")
    op.execute("ALTER TABLE source_sweep DROP COLUMN IF EXISTS blocked")
