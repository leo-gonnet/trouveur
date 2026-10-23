"""The board trial: a discovered board is swept once, and keeps itself if it had a local job.

Discovery proposes thousands of boards and most of them hire nowhere near any reader. Sweeping
all of them nightly would make the scan take hours; leaving them switched off until somebody
reads a list means nobody ever does, and the boards a reader's own archive pointed at sit there
unswept.

So the sweep decides, on evidence it already holds: one trial sweep, and a board with an open
posting inside some reader's area is swept nightly from then on. `trial_at` is what makes that
bounded -- it is the last time we tried a board and judged it, so "due for a trial" is a date
comparison and the cap per run is a LIMIT. It is observation and lives here rather than on
`source_tenant`, which is the crawl set: what we DID, not what we decided.

Nothing is ever switched off by this. A board that had a local posting and has none today is
still the board that will post the next one, and a crawl set that shrinks on its own shrinks
silently -- the same reason a failure streak does not retire a board either.

Revision ID: 0023_board_trial
"""

from alembic import op

revision = "0023_board_trial"
down_revision = "0022_user_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE source_scope_health ADD COLUMN trial_at timestamptz")
    # The trial queue: candidates a discovery pass proposed and nobody has decided about. Partial,
    # because the enabled boards are the overwhelming majority of the table and none of them is
    # ever a candidate again.
    op.execute(
        "CREATE INDEX source_tenant_candidate_idx ON source_tenant (source, scope) "
        "WHERE origin = 'discovered' AND NOT enabled AND dropped_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS source_tenant_candidate_idx")
    op.execute("ALTER TABLE source_scope_health DROP COLUMN IF EXISTS trial_at")
