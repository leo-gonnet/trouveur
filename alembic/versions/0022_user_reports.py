"""A reader's report of a job they found somewhere else, and what we could tell them about it.

The one measure of recall on jobs somebody actually wanted. Everything else we can say about
recall compares us against ourselves; this compares us against a job a reader went and found.

Per reader and not per link, which is why this is not a column on `discovery_lead`: the link is
one row there however many people paste it, and the answer is different for each of them -- the
same posting is in one reader's area and outside another's. The lead is still written, because the
board behind the link is worth having whatever the answer turns out to be.

`outcome` and `reason` are frozen with the answer rather than recomputed on the page, the same
way an edition is. A reader asked on a day, and the answer they were given has to keep saying
what it said: the board gets promoted, the posting arrives in next week's sweep, and an answer
recomputed later would claim we held something we did not hold when they asked.

Revision ID: 0022_user_reports
"""

from alembic import op

revision = "0022_user_reports"
down_revision = "0021_discovery_leads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE report_outcome AS ENUM ('had_and_recommended','had_not_recommended',"
        "'missing_job','missing_board','unknown_platform')"
    )
    op.execute("CREATE TYPE report_reason AS ENUM ('location','not_retrieved','not_scored')")
    op.execute(
        """
        CREATE TABLE user_report (
            id          bigserial PRIMARY KEY,
            user_id     bigint NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
            url         text NOT NULL,
            job_id      bigint REFERENCES job (id) ON DELETE SET NULL,
            source      text,
            scope       text,
            outcome     report_outcome,
            reason      report_reason,
            created_at  timestamptz NOT NULL DEFAULT now(),
            answered_at timestamptz
        )
        """
    )
    # The queue: a report with no outcome is one the runner still owes an answer for. A version
    # column would be wrong here -- re-answering is exactly what must not happen.
    op.execute("CREATE INDEX user_report_pending_idx ON user_report (id) WHERE outcome IS NULL")
    # The reader's own list, newest first.
    op.execute("CREATE INDEX user_report_user_idx ON user_report (user_id, id DESC)")
    # One open question per link per reader. A double-pressed button, or the same link pasted
    # again while the first is still waiting, is one question and not two; once it is answered the
    # same link may be asked again, and the new answer stands beside the old one.
    op.execute(
        "CREATE UNIQUE INDEX user_report_open_uniq ON user_report (user_id, url) "
        "WHERE outcome IS NULL"
    )
    # Nothing indexes `outcome` itself: the coverage report groups the whole table, which holds
    # one row per link a reader went to the trouble of pasting.


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_report")
    op.execute("DROP TYPE IF EXISTS report_reason")
    op.execute("DROP TYPE IF EXISTS report_outcome")
