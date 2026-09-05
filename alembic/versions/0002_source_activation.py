"""source activation

Sources are opt-in: a scan only runs the sources listed here as enabled, and a fresh install has
none. Its own table rather than a profile column for the same reason as personio_tenant --
switching a source on must not bump profile.version and invalidate every cached LLM score.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No seed row on purpose: an empty table means nothing is activated, which is the intended
    # state of a fresh install.
    op.execute(
        """
        CREATE TABLE source_activation (
            source     text PRIMARY KEY,
            enabled    boolean NOT NULL DEFAULT false,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS source_activation")
