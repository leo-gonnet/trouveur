"""Leads from board lists and from freehire, and what their source said beyond the link.

`payload` is a small extract, never a raw page: neither origin is archived, so it is what a later
resolver can re-read without asking anybody again.

Revision ID: 0027_list_leads
"""

from alembic import op

revision = "0027_list_leads"
down_revision = "0026_blocks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE lead_origin ADD VALUE IF NOT EXISTS 'list'")
    op.execute("ALTER TYPE lead_origin ADD VALUE IF NOT EXISTS 'freehire'")
    op.execute("ALTER TABLE discovery_lead ADD COLUMN list_name text")
    op.execute("ALTER TABLE discovery_lead ADD COLUMN payload jsonb")


def downgrade() -> None:
    op.execute("DELETE FROM discovery_lead WHERE origin IN ('list', 'freehire')")
    op.execute("ALTER TABLE discovery_lead DROP COLUMN IF EXISTS payload")
    op.execute("ALTER TABLE discovery_lead DROP COLUMN IF EXISTS list_name")
    op.execute("ALTER TYPE lead_origin RENAME TO lead_origin_old")
    op.execute("CREATE TYPE lead_origin AS ENUM ('archive','user_report')")
    op.execute(
        "ALTER TABLE discovery_lead ALTER COLUMN origin TYPE lead_origin "
        "USING origin::text::lead_origin"
    )
    op.execute("DROP TYPE lead_origin_old")
