"""Store editions instead of deriving them from scored_at, and replace rerank_limit with a switch.

An edition used to be a GROUP BY over `user_job_match.scored_at::date`. A row carries one
scored_at, so re-scoring a posting moved it out of the day it was published in and into the day it
was re-scored: yesterday's page silently lost a row. Worse, `users.reset_scores` nulled llm_score
and scored_at for every row whenever a scoring field changed, which erased the user's whole
recommendation history the moment they edited their profile -- while the page promised the
opposite. An edition is a published record, so it is now stored as one and never updated.

The backfill reads the old derivation once, so nobody loses the history they already had.

rerank_limit went three ways at once: the length of the list, the size of the bill, and (at 0) the
pause. Length is now a constant and the bill is capped by monthly_budget_usd, which was always the
real ceiling; what is left is the pause, which says what it does.

Revision ID: 0012_immutable_editions
"""

from alembic import op

revision = "0012_immutable_editions"
down_revision = "0011_document_fk_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_edition_item (
            user_id bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            day date NOT NULL,
            job_id bigint NOT NULL REFERENCES job(id) ON DELETE CASCADE,
            profile_version integer NOT NULL,
            llm_score smallint NOT NULL,
            llm_reason text NOT NULL DEFAULT '',
            llm_red_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, day, job_id),
            CONSTRAINT uq_edition_item_once_per_version
                UNIQUE (user_id, job_id, profile_version)
        )
        """
    )
    # The page opens on one day at a time, newest first.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_user_edition_item_user_day
            ON user_edition_item (user_id, day DESC)
        """
    )

    # Carry the derived editions over before the derivation stops being read. Every (user, job)
    # is one row upstream, so no two rows can collide on the version constraint.
    op.execute(
        """
        INSERT INTO user_edition_item
            (user_id, day, job_id, profile_version, llm_score, llm_reason, llm_red_flags)
        SELECT user_id, scored_at::date, job_id, profile_version, llm_score,
               coalesce(llm_reason, ''), coalesce(llm_red_flags, '[]'::jsonb)
        FROM user_job_match
        WHERE llm_score IS NOT NULL AND scored_at IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )

    op.execute(
        """
        ALTER TABLE user_profile
            ADD COLUMN IF NOT EXISTS scoring_enabled boolean NOT NULL DEFAULT true
        """
    )
    # 0 was the pause, and the only setting that could mean it. Carry that across rather than
    # silently switching those users' spending back on.
    op.execute("UPDATE user_profile SET scoring_enabled = false WHERE rerank_limit = 0")
    op.execute("ALTER TABLE user_profile DROP COLUMN IF EXISTS rerank_limit")


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_profile
            ADD COLUMN IF NOT EXISTS rerank_limit integer NOT NULL DEFAULT 150
        """
    )
    op.execute("UPDATE user_profile SET rerank_limit = 0 WHERE NOT scoring_enabled")
    op.execute("ALTER TABLE user_profile DROP COLUMN IF EXISTS scoring_enabled")
    op.execute("DROP TABLE IF EXISTS user_edition_item")
