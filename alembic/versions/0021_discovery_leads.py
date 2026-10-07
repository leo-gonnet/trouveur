"""Leads: one job seen somewhere, and the board its link turned out to address.

Discovery needs a place to put a job it has seen but not collected. A lead is that place: where we
saw it, the link, and what the URL rules made of the link. One lead can bring a whole board, so
one Java advert on an aggregator can bring the company's other forty postings with it.

The table is a derived stage like any other, which is what `resolve_version` buys: the rules are a
pure function of the URL, so bumping the version re-reads every lead we already hold, and the day
an adapter for a new platform ships, the boards for it are already known without a single request.

`job.mined_version` is the other half, on `job` beside `normalize_version` and `dedup_version`
rather than in a table of its own: mining reads a posting we already hold, and a posting is mined
once per version. The index is what makes "every posting below the current version" a keyset walk
instead of a sequential scan of the largest table we have.

There is no `duplicate` result, although the plan named one. Whether a lead's board is one we
already sweep is a join against `source_tenant` -- it changes the day somebody promotes the board,
so frozen into the row it would be a verdict that reads as fact while being out of date. The same
goes for a lead arriving twice: that is the unique index below, not something to record.

Revision ID: 0021_discovery_leads
"""

from alembic import op

revision = "0021_discovery_leads"
down_revision = "0020_tenant_dropped"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE lead_origin AS ENUM ('archive','user_report')")
    op.execute("CREATE TYPE lead_result AS ENUM ('resolved','unknown_host','no_url')")
    op.execute(
        """
        CREATE TABLE discovery_lead (
            id             bigserial PRIMARY KEY,
            origin         lead_origin NOT NULL,
            url            text NOT NULL DEFAULT '',
            host           text,
            job_id         bigint REFERENCES job (id) ON DELETE SET NULL,
            company        text,
            title          text,
            location_text  text,
            result         lead_result NOT NULL,
            source         text,
            scope          text,
            resolve_version integer NOT NULL DEFAULT 0,
            first_seen_at  timestamptz NOT NULL DEFAULT now(),
            resolved_at    timestamptz
        )
        """
    )
    # One row per link per origin. PARTIAL, because a lead can name no link at all (a reader
    # reporting a job they saw and could not link to): those all carry url = '', and a plain
    # unique index would fold every one of them into a single row and lose the rest in silence.
    op.execute(
        "CREATE UNIQUE INDEX discovery_lead_url_uniq ON discovery_lead (origin, url) "
        "WHERE url <> ''"
    )
    # The resolver's own queue: every lead below the shipped version, in keyset order.
    op.execute("CREATE INDEX discovery_lead_stale_idx ON discovery_lead (resolve_version, id)")
    # Promoting candidates is DISTINCT over the boards we can sweep, which this answers without
    # touching the table. Partial, because the other three results carry no board at all.
    op.execute(
        "CREATE INDEX discovery_lead_board_idx ON discovery_lead (source, scope) "
        "WHERE result = 'resolved'"
    )
    # What the coverage report groups by: the hosts whose links we could not read. Measured, not
    # assumed -- with a realistic mix (most leads unreadable company careers pages, the rest on
    # boards we sweep) the planner takes a bitmap scan on this; with `source` in the middle it
    # cannot, and nothing groups leads by platform.
    op.execute("CREATE INDEX discovery_lead_unread_idx ON discovery_lead (result, host)")

    op.execute("ALTER TABLE job ADD COLUMN mined_version smallint NOT NULL DEFAULT 0")
    op.execute("CREATE INDEX job_mined_idx ON job (mined_version, id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS job_mined_idx")
    op.execute("ALTER TABLE job DROP COLUMN IF EXISTS mined_version")
    op.execute("DROP TABLE IF EXISTS discovery_lead")
    op.execute("DROP TYPE IF EXISTS lead_result")
    op.execute("DROP TYPE IF EXISTS lead_origin")
