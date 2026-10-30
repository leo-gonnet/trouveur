"""What the corpus holds for one area, and what state the crawl set is in.

Answers "did that new source help", which no other query does: the Operations panels above it
count the whole corpus, and a source that doubled the postings in Hamburg moves them by a few
per cent. Everything here is read-only.
"""

from __future__ import annotations

from collections.abc import Sequence
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
#
# `discovered` is the one number that says whether discovery and the board trial are earning their
# keep: postings in this area from a board nobody registered by hand. A global source has no
# `source_tenant` row at all, so the LEFT JOIN leaves it NULL and it counts as not discovered,
# which is right -- nobody discovered it.
_BY_SOURCE = f"""
WITH area AS (
    SELECT j.id, j.source, j.dedup_group,
           ({freshness.sql("j")} AND e.embedding_768 IS NOT NULL
            AND NOT {location.CLOSED_AT_ORIGIN}) AS retrievable,
           (t.origin = 'discovered') AS discovered
    FROM job j
    JOIN job_facet f ON f.job_id = j.id
    LEFT JOIN job_embedding e ON e.job_id = j.id
    LEFT JOIN source_tenant t ON t.source = j.source AND t.scope = j.scope
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
       count(*) FILTER (WHERE coalesce(g.sources, 1) = 1) AS only_source,
       count(*) FILTER (WHERE a.discovered) AS discovered_jobs
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


# Five states, and they partition the rows: a table of states whose columns do not add up to the
# boards registered is one a reader cannot check. `enabled` decides first, because it is the
# operative fact -- what the sweep actually does -- and `dropped_at` only says why a board that is
# off is off. It also beats `origin`: a candidate somebody tried and rejected is not waiting for a
# trial any more.
#
# `auto_on` is a subset of `sweeping` and deliberately not a state of its own: it is how many of
# the boards being swept got there without a human, which is the whole measure of the board trial.
#
# `disabled` is a board a person switched off, whatever found it: counting a disabled discovered
# board under `monthly` showed it as still waiting for a trial it will never get.
#
# `to_try` and `monthly` split what used to be one `candidates` column, because they mean opposite
# things now. `to_try` is a board nothing has asked yet -- it will be swept within a few runs.
# `monthly` is a board we asked, that had nothing for any reader, and that is asked again every
# `trial_recheck_days`: not rejected, just not worth a nightly request. A `monthly` column that
# keeps growing while `auto_on` does not is discovery finding boards in the wrong part of the
# world, which is a lead-source problem and not a trial problem.
_TENANT_STATES = """
SELECT t.source,
       count(*) FILTER (WHERE t.enabled) AS sweeping,
       count(*) FILTER (WHERE t.enabled AND t.origin = 'discovered') AS auto_on,
       count(*) FILTER (
           WHERE NOT t.enabled AND t.dropped_at IS NULL AND t.origin = 'discovered'
             AND t.decided_at IS NULL AND h.trial_at IS NULL) AS to_try,
       count(*) FILTER (
           WHERE NOT t.enabled AND t.dropped_at IS NULL AND t.origin = 'discovered'
             AND t.decided_at IS NULL AND h.trial_at IS NOT NULL) AS monthly,
       count(*) FILTER (
           WHERE NOT t.enabled AND t.dropped_at IS NULL
             AND (t.origin = 'manual' OR t.decided_at IS NOT NULL)) AS disabled,
       count(*) FILTER (WHERE NOT t.enabled AND t.dropped_at IS NOT NULL) AS dropped
FROM source_tenant t
LEFT JOIN source_scope_health h ON h.source = t.source AND h.scope = t.scope
GROUP BY t.source
ORDER BY t.source
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


# What readers reported finding elsewhere, by the answer they got. One row, over a table that
# holds one row per link somebody went to the trouble of pasting, so nothing indexes it.
#
# The reasons are counted beside the outcomes rather than looked up separately: a dozen reports
# saying "we had it and never showed it" says nothing until you know whether the filter, the
# retrieval or the scoring stopped them.
_REPORT_OUTCOMES = """
SELECT count(*) AS reports,
       count(*) FILTER (WHERE outcome IS NULL) AS waiting,
       count(*) FILTER (WHERE outcome = 'had_and_recommended') AS recommended,
       count(*) FILTER (WHERE outcome = 'had_not_recommended') AS not_recommended,
       count(*) FILTER (WHERE outcome = 'missing_job') AS missing_job,
       count(*) FILTER (WHERE outcome = 'missing_board') AS missing_board,
       count(*) FILTER (WHERE outcome = 'unknown_platform') AS unknown_platform,
       count(*) FILTER (WHERE reason = 'location') AS by_location,
       count(*) FILTER (WHERE reason = 'not_retrieved') AS by_retrieval,
       count(*) FILTER (WHERE reason = 'not_scored') AS by_scoring
FROM user_report
"""


async def report_outcomes(conn: AsyncConnection) -> sa.Row:
    """Every reported link by what we could tell the reader. The recall measure that counts."""
    return (await conn.execute(sa.text(_REPORT_OUTCOMES))).one()


# Per aggregator, the jobs it showed us this window, how many we could judge, and how many we
# already held when it did: recall against what a reader would have found by searching that site.
_RECALL = """
SELECT origin::text AS origin,
       count(*) AS seen,
       count(*) FILTER (WHERE held IS NOT NULL) AS judged,
       count(*) FILTER (WHERE held) AS held
FROM discovery_lead
WHERE origin::text = ANY(CAST(:origins AS text[]))
  AND first_seen_at >= now() - make_interval(days => :days)
GROUP BY origin
ORDER BY origin
"""


async def recall(conn: AsyncConnection, origins: Sequence[str], days: int) -> list[sa.Row]:
    return list(
        await conn.execute(sa.text(_RECALL), {"origins": list(origins), "days": days})
    )


# The companies behind the misses, most missed first: the next board to add is a company's. The
# totals ride on every row, counted before the LIMIT.
_MISSES = """
SELECT min(company) AS company, count(*) AS jobs, min(title) AS sample_title,
       sum(count(*)) OVER () AS total_jobs, count(*) OVER () AS total_companies
FROM discovery_lead
WHERE origin::text = ANY(CAST(:origins AS text[]))
  AND held = false
  AND first_seen_at >= now() - make_interval(days => :days)
GROUP BY lower(company)
ORDER BY jobs DESC, lower(company)
LIMIT :limit
"""


async def misses(
    conn: AsyncConnection, origins: Sequence[str], days: int, limit: int
) -> list[sa.Row]:
    params = {"origins": list(origins), "days": days, "limit": limit}
    return list(await conn.execute(sa.text(_MISSES), params))


# Sources whose latest finished sweep the provider refused.
_BLOCKED = """
SELECT source, started_at, error
FROM (
    SELECT DISTINCT ON (source) source, started_at, blocked, error
    FROM source_sweep
    WHERE finished_at IS NOT NULL
    ORDER BY source, started_at DESC
) latest
WHERE blocked
ORDER BY source
"""


async def blocked_sources(conn: AsyncConnection) -> list[sa.Row]:
    return list(await conn.execute(sa.text(_BLOCKED)))
