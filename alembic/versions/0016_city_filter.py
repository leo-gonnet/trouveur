"""Cities join the location filter: resolved towns on facets, picked towns and a radius on profiles.

A city could not be filtered while it was stored as the source spelled it: `Wien`, `Vienna` and
`Wien 10., Favoriten` were three different values, and a filter on one silently dropped the other
two. Derivation now resolves a town to one GeoNames id (`place_ids`), and records the country of
every location whose town it could not resolve (`unplaced_countries`), so the filter can still
judge that posting by its country instead of dropping it. Both are filled by the re-derive that
DERIVE_VERSION 4 queues.

On the profile, `cities` was free text read only by the reranker. It becomes `city_ids`, picked
from the same list, with one `radius_km` around them. The old free-text values cannot be turned
into ids without guessing, so they are dropped, not converted.

Revision ID: 0016_city_filter
"""

import sqlalchemy as sa

from alembic import op

revision = "0016_city_filter"
down_revision = "0015_versioned_editions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_facet",
        sa.Column("place_ids", sa.ARRAY(sa.Integer()), nullable=False, server_default="{}"),
    )
    op.add_column(
        "job_facet",
        sa.Column(
            "unplaced_countries", sa.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "user_profile",
        sa.Column("city_ids", sa.ARRAY(sa.Integer()), nullable=False, server_default="{}"),
    )
    op.add_column(
        "user_profile",
        sa.Column("radius_km", sa.Integer(), nullable=False, server_default="30"),
    )
    op.drop_column("user_profile", "cities")


def downgrade() -> None:
    op.add_column(
        "user_profile",
        sa.Column("cities", sa.ARRAY(sa.Text()), nullable=False, server_default="{}"),
    )
    op.drop_column("user_profile", "radius_km")
    op.drop_column("user_profile", "city_ids")
    op.drop_column("job_facet", "unplaced_countries")
    op.drop_column("job_facet", "place_ids")
