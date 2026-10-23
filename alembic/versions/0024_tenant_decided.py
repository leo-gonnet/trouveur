"""A person decided about this board, so the trial leaves it alone.

The trial queue read "nobody has decided" as `NOT enabled AND dropped_at IS NULL`, and that is
not what it means: `tenants disable` leaves `dropped_at` NULL, so a disabled board was swept on
the next run and switched straight back on. `decided_at` is configuration and not observation, so
it lives here beside `enabled` rather than on `source_scope_health`.

NOT backfilled. Every discovered row today is a candidate from the design where a human promoted
each one, and stamping those would freeze the whole pool unjudged.

Revision ID: 0024_tenant_decided
"""

from alembic import op

revision = "0024_tenant_decided"
down_revision = "0023_board_trial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE source_tenant ADD COLUMN decided_at timestamptz")
    # In the index rather than filtered after it: this is the trial queue's own index.
    op.execute("DROP INDEX IF EXISTS source_tenant_candidate_idx")
    op.execute(
        "CREATE INDEX source_tenant_candidate_idx ON source_tenant (source, scope) "
        "WHERE origin = 'discovered' AND NOT enabled AND dropped_at IS NULL "
        "AND decided_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS source_tenant_candidate_idx")
    op.execute(
        "CREATE INDEX source_tenant_candidate_idx ON source_tenant (source, scope) "
        "WHERE origin = 'discovered' AND NOT enabled AND dropped_at IS NULL"
    )
    op.execute("ALTER TABLE source_tenant DROP COLUMN IF EXISTS decided_at")
