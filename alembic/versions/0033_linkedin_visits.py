"""LinkedIn as visits spread over the day: each reader's guest cookie jar and visit clock, every
request it sent, and the cards it saw waiting for the nightly judgement.

Revision ID: 0033_linkedin_visits
"""

from alembic import op

revision = "0033_linkedin_visits"
down_revision = "0032_recall_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE linkedin_visitor (
            user_id        bigint PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
            cookies        jsonb NOT NULL DEFAULT '[]',
            jar_expires_at timestamptz NOT NULL,
            next_visit_at  timestamptz NOT NULL,
            next_search    integer NOT NULL DEFAULT 0,
            updated_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE linkedin_request (
            id          bigserial PRIMARY KEY,
            sweep_id    bigint NOT NULL,
            user_id     bigint NOT NULL,
            sent_at     timestamptz NOT NULL,
            kind        text NOT NULL,
            status      integer,
            elapsed_ms  integer NOT NULL,
            cards       integer NOT NULL DEFAULT 0,
            retry_after text,
            set_cookies text[] NOT NULL DEFAULT '{}',
            error       text
        )
        """
    )
    op.execute("CREATE INDEX linkedin_request_sent_idx ON linkedin_request (sent_at)")
    op.execute(
        """
        CREATE TABLE linkedin_seen (
            linkedin_id   text PRIMARY KEY,
            company       text,
            title         text,
            location_text text,
            seen_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE linkedin_seen")
    op.execute("DROP TABLE linkedin_request")
    op.execute("DROP TABLE linkedin_visitor")
