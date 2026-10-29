"""The daily run time moves to config, and runs lose the options only the removed form set.

Revision ID: 0030_schedule_in_config
"""

from alembic import op

revision = "0030_schedule_in_config"
down_revision = "0029_run_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE pipeline_schedule")
    op.execute("ALTER TABLE pipeline_run DROP COLUMN only_source, DROP COLUMN backfill")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE pipeline_run ADD COLUMN only_source text, "
        "ADD COLUMN backfill boolean NOT NULL DEFAULT false"
    )
    op.execute(
        """
        CREATE TABLE pipeline_schedule (
            id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            enabled boolean NOT NULL DEFAULT true,
            run_hour smallint NOT NULL DEFAULT 7,
            run_minute smallint NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("INSERT INTO pipeline_schedule (id, run_hour, run_minute) VALUES (1, 4, 30)")
