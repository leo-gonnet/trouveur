"""A day holds one edition per profile version, and no edition is ever destroyed.

Changing a profile used to REPLACE the day's edition: the run deleted the rows published under the
old version and wrote new ones. That is why the Profile form had a confirm page -- saving destroyed
something, so the user had to be asked. It was also the one exception to "an edition is never
rewritten", and an exception in an invariant is the thing that eventually gets taken for the rule.

Adding profile_version to the key removes the exception instead of guarding it. A profile change
publishes a SECOND edition for today, beside the first, and the dropdown lists both. Nothing is
deleted, so nothing needs confirming, and "an edition is never rewritten" becomes true without
qualification.

`uq_edition_item_once_per_version` is untouched and still does its own job: one posting reaches one
user once per profile version, ever, across all days.

Revision ID: 0015_versioned_editions
"""

from alembic import op

revision = "0015_versioned_editions"
down_revision = "0014_location_only_filter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_edition_item DROP CONSTRAINT user_edition_item_pkey")
    op.execute(
        "ALTER TABLE user_edition_item ADD PRIMARY KEY (user_id, day, profile_version, job_id)"
    )


def downgrade() -> None:
    # Lossy by construction: a day that holds two versions cannot fit a three-column key, so the
    # later version's rows are dropped, which is what the old code would have done at publish.
    op.execute(
        """
        DELETE FROM user_edition_item a
        USING user_edition_item b
        WHERE a.user_id = b.user_id AND a.day = b.day AND a.job_id = b.job_id
          AND a.profile_version < b.profile_version
        """
    )
    op.execute("ALTER TABLE user_edition_item DROP CONSTRAINT user_edition_item_pkey")
    op.execute("ALTER TABLE user_edition_item ADD PRIMARY KEY (user_id, day, job_id)")
