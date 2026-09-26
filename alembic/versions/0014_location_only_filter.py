"""Location is the only hard filter: drop the work-mode, seniority and employment-type lists.

Each of those three filtered on an enum whose UNKNOWN member is common, so setting one dropped
every posting that stated nothing for it -- which is why the form had to offer a "not stated" tick
box beside each, a control whose only purpose was to undo the filter the user had just set. The
same went for the salary floor, which needed an `IS NULL` escape in the SQL for the same reason.
None of the four were narrowing the search usefully: seniority is already said by the title and the
years of experience, employment type is already penalised in the reranker's system prompt, and
salary is a preference the prompt has always carried.

What replaces them is one boolean that belongs to location rather than to work mode. A fully remote
role is not in a third kind of place, it is in none, so the country a posting names says nothing
about whether the reader can take it; `remote_anywhere` admits those wherever they were posted, and
the description -- which the reranker reads -- is the only place the real restriction is written.
Onsite and hybrid are no longer distinguished anywhere, deliberately: both mean "you go there",
which the country already says.

The three columns are dropped rather than left unread, so nothing can quietly start filtering on
them again. No profile version is bumped here: widening the filter can only admit postings, and an
admitted posting has no score yet, so it is scored on the next run without invalidating anything.

Revision ID: 0014_location_only_filter
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_location_only_filter"
down_revision = "0013_exact_knn"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_profile",
        sa.Column("remote_anywhere", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.drop_column("user_profile", "work_modes")
    op.drop_column("user_profile", "seniorities")
    op.drop_column("user_profile", "employment_types")


def downgrade() -> None:
    for name in ("work_modes", "seniorities", "employment_types"):
        op.add_column(
            "user_profile",
            sa.Column(name, sa.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        )
    op.drop_column("user_profile", "remote_anywhere")
