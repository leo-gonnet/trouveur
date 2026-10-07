"""What the corpus holds for one area, and what state the crawl set is in.

Answers "did that new source help", which no other query does: the Operations panels above it
count the whole corpus, and a source that doubled the postings in Hamburg moves them by a few
per cent. Everything here is read-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.queries import freshness, location

# `only_source` is "if this source went away, the area would lose this posting entirely": a
# posting whose dedup_group holds nothing from another source. Twins are looked for inside the
# SAME area, because an identical role at the company's other office is not a replacement for
# this one.
#
# A posting with no dedup_group has not been through the dedup queue yet, so by the markers we
# hold nothing else is known to have it, and it counts as exclusive. It is NOT grouped with the
# other unmarked rows -- `GROUP BY dedup_group` would put every NULL in one bucket and report
# the whole backlog as shared by everybody. The queue depth panel is where a backlog shows.
#
# `retrievable` is what the dense arm could actually return today: inside the horizon and
# carrying a vector. The gap between it and `open_jobs` is a posting collected but invisible to
# recommendations, which otherwise reads as poor recall.
_BY_SOURCE = f"""
WITH area AS (
    SELECT j.id, j.source, j.dedup_group,
           ({freshness.sql("j")} AND e.embedding_768 IS NOT NULL) AS retrievable
    FROM job j
    JOIN job_facet f ON f.job_id = j.id
    LEFT JOIN job_embedding e ON e.job_id = j.id
    WHERE j.closed_at IS NULL
      AND {location.SQL}
),
groups AS (
    SELECT dedup_group, count(DISTINCT source) AS sources
    FROM area
    WHERE dedup_group IS NOT NULL
    GROUP BY dedup_group
)
SELECT a.source,
       count(*) AS open_jobs,
       count(*) FILTER (WHERE a.retrievable) AS retrievable_jobs,
       count(*) FILTER (WHERE coalesce(g.sources, 1) = 1) AS only_source
FROM area a
LEFT JOIN groups g ON g.dedup_group = a.dedup_group
GROUP BY a.source
ORDER BY open_jobs DESC, a.source
"""


async def by_source(
    conn: AsyncConnection, area: Any, fresh_since: datetime
) -> list[sa.Row]:
    """Open, retrievable and irreplaceable postings per source, for one area."""
    return list(
        await conn.execute(
            sa.text(_BY_SOURCE), {"fresh_since": fresh_since, **location.params(area)}
        )
    )


# Four states from two columns, and they partition the rows: a table of states whose columns do
# not add up to the boards registered is one a reader cannot check. `enabled` decides first,
# because it is the operative fact -- what the sweep actually does -- and `dropped_at` only says
# why a board that is off is off. It also beats `origin`: a candidate somebody tried and rejected
# is not awaiting review any more.
_TENANT_STATES = """
SELECT source,
       count(*) FILTER (WHERE enabled) AS sweeping,
       count(*) FILTER (
           WHERE NOT enabled AND dropped_at IS NULL AND origin = 'discovered') AS candidates,
       count(*) FILTER (
           WHERE NOT enabled AND dropped_at IS NULL AND origin = 'manual') AS disabled,
       count(*) FILTER (WHERE NOT enabled AND dropped_at IS NOT NULL) AS dropped
FROM source_tenant
GROUP BY source
ORDER BY source
"""


async def tenant_states(conn: AsyncConnection) -> list[sa.Row]:
    """The crawl set per source by state. Not per area: a board is swept for everybody."""
    return list(await conn.execute(sa.text(_TENANT_STATES)))


# One row per active reader, deliberately ungrouped. Two readers who picked the same area are
# one row in the report, but `['AT','DE']` and `['DE','AT']` are the same area and two different
# array values, so grouping here would miss the pair; the caller sorts the lists into a key and
# groups once. Grouping in both places is one idea implemented twice.
_AREAS = """
SELECT p.countries, p.city_ids, p.radius_km, p.remote_anywhere
FROM user_profile p
JOIN app_user u ON u.id = p.user_id
WHERE u.is_active
"""


async def areas(conn: AsyncConnection) -> list[sa.Row]:
    """Every active reader's area, one row each. The report names no city of its own."""
    return list(await conn.execute(sa.text(_AREAS)))
