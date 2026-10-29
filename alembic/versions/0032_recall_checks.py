"""Nightly recall checks: did search still find the jobs the reader saved or applied to?

Revision ID: 0032_recall_checks
"""

from alembic import op

revision = "0032_recall_checks"
down_revision = "0031_run_stage_times"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE recall_check (
            id              bigserial PRIMARY KEY,
            user_id         bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            profile_version integer NOT NULL,
            checked_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX recall_check_user_idx ON recall_check (user_id, checked_at)")
    op.execute(
        """
        CREATE TABLE recall_needle (
            check_id bigint NOT NULL REFERENCES recall_check(id) ON DELETE CASCADE,
            job_id   bigint NOT NULL REFERENCES job(id) ON DELETE CASCADE,
            rank     integer,
            PRIMARY KEY (check_id, job_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE recall_needle")
    op.execute("DROP TABLE recall_check")
