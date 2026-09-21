"""A candidate's background, and the summary both prompts read.

A profile said what someone wants and almost nothing about what they have done. Expansion had to
invent a plausible next role from a job title alone -- and, measured, the pinned model wrote
adverts for the role the candidate already has, which is exactly wrong for a career change. The
reranker had to judge fit against a paragraph of aspiration with no evidence of capability.

Two columns, because the raw text and the text a prompt sees are not the same thing:

  - `user_profile.background` is what the user typed. It is a scoring field, so editing it bumps
    the profile version and clears that user's cached scores.
  - `user_query_expansion.background_summary` is that text distilled once per profile version and
    reused by every batch afterwards. It lives beside the queries and the adverts because it is
    the same kind of artifact -- derived from one profile version, cached under it, recomputed
    when it changes. Sending the raw CV instead would multiply its tokens across every scored
    posting and let a long one crowd out the objectives.

Revision ID: 0010_profile_background
"""

from alembic import op

revision = "0010_profile_background"
down_revision = "0009_export_cursors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_profile
            ADD COLUMN IF NOT EXISTS background text NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        ALTER TABLE user_query_expansion
            ADD COLUMN IF NOT EXISTS background_summary text NOT NULL DEFAULT ''
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE user_query_expansion DROP COLUMN IF EXISTS background_summary")
    op.execute("ALTER TABLE user_profile DROP COLUMN IF EXISTS background")
