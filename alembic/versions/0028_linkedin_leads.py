"""Leads from LinkedIn's guest search, and whether we already held each job when we saw it.

Revision ID: 0028_linkedin_leads
"""

from alembic import op

revision = "0028_linkedin_leads"
down_revision = "0027_list_leads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE lead_origin ADD VALUE IF NOT EXISTS 'linkedin'")
    op.execute("ALTER TABLE discovery_lead ADD COLUMN held boolean")
    op.execute("ALTER TABLE discovery_lead ADD COLUMN held_job_id bigint")


def downgrade() -> None:
    op.execute("DELETE FROM discovery_lead WHERE origin = 'linkedin'")
    op.execute("ALTER TABLE discovery_lead DROP COLUMN IF EXISTS held_job_id")
    op.execute("ALTER TABLE discovery_lead DROP COLUMN IF EXISTS held")
    op.execute("ALTER TYPE lead_origin RENAME TO lead_origin_old")
    op.execute("CREATE TYPE lead_origin AS ENUM ('archive','user_report','list','freehire')")
    op.execute(
        "ALTER TABLE discovery_lead ALTER COLUMN origin TYPE lead_origin "
        "USING origin::text::lead_origin"
    )
    op.execute("DROP TYPE lead_origin_old")
