"""SQL for the offsite corpus export.

Reads only, in arrival order, so a day is a contiguous slice that never changes again: a day is a
file, written once, and "which days are uploaded" can be read back from the destination.

Types are cast to text in the query, not converted in Python -- the archive outlives this schema.
"""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

# Deliberately absent below: the generated search columns and job_embedding (both recomputable),
# and every user_* table -- the corpus is public job postings, a person's account is not.

# One aggregate per stream rather than a probe per day, so a day that never had anything is
# absent instead of being re-checked every night for ever.
_DOCUMENT_COUNTS = """
SELECT fetched_at::date AS day, count(*) AS rows FROM source_document GROUP BY 1
"""

_JOB_COUNTS = """
SELECT first_seen_at::date AS day, count(*) AS rows FROM job GROUP BY 1
"""

_CLOSURE_COUNTS = """
SELECT closed_at::date AS day, count(*) AS rows FROM job
WHERE closed_at IS NOT NULL GROUP BY 1
"""

_DOCUMENTS = """
SELECT id,
       source,
       external_id,
       kind::text AS kind,
       scope,
       payload::text AS payload,
       encode(payload_sha256, 'hex') AS payload_sha256,
       fetched_at
FROM source_document
WHERE fetched_at >= CAST(:day AS date)
  AND fetched_at < CAST(:day AS date) + 1
  AND id > :after
ORDER BY id
LIMIT :chunk
"""

_JOBS = """
SELECT j.id,
       j.public_id::text AS public_id,
       j.source,
       j.external_id,
       j.scope,
       j.url,
       j.title,
       j.company,
       j.description,
       j.posted_at,
       j.updated_at,
       j.closes_at,
       j.locations::text AS locations,
       j.location_text,
       j.salary_amount_min::float8 AS salary_amount_min,
       j.salary_amount_max::float8 AS salary_amount_max,
       j.salary_currency,
       j.salary_period::text AS salary_period,
       j.remote_hint,
       j.employment_type_hint,
       j.agency_hint,
       j.language_hint,
       j.department_hint,
       encode(j.content_hash, 'hex') AS content_hash,
       j.normalize_version,
       j.listing_document_id,
       j.detail_document_id,
       j.first_seen_at,
       j.last_seen_at,
       j.closed_at,
       encode(j.dedup_group, 'hex') AS dedup_group,
       j.dedup_version,
       f.derive_version,
       f.countries,
       f.regions,
       f.cities,
       f.work_mode::text AS work_mode,
       f.seniority::text AS seniority,
       f.employment_type::text AS employment_type,
       f.salary_min_eur_year::float8 AS salary_min_eur_year,
       f.salary_max_eur_year::float8 AS salary_max_eur_year,
       f.salary_annualised,
       f.language,
       f.is_agency,
       f.skills,
       f.derived_at
FROM job j
LEFT JOIN job_facet f ON f.job_id = j.id
WHERE j.first_seen_at >= CAST(:day AS date)
  AND j.first_seen_at < CAST(:day AS date) + 1
  AND j.id > :after
ORDER BY j.id
LIMIT :chunk
"""

# A day partition of `job` cannot carry closures: a posting ingested in March and retired in
# September would change a file written six months earlier. So retirement is its own stream.
_CLOSURES = """
SELECT id AS job_id,
       source,
       external_id,
       last_seen_at,
       closed_at
FROM job
WHERE closed_at >= CAST(:day AS date)
  AND closed_at < CAST(:day AS date) + 1
  AND id > :after
ORDER BY id
LIMIT :chunk
"""


async def _counts(conn: AsyncConnection, sql: str) -> dict[date, int]:
    return {row.day: row.rows for row in await conn.execute(sa.text(sql))}


async def document_counts(conn: AsyncConnection) -> dict[date, int]:
    """Payloads per day the archive was fetched on. Empty if the archive is."""
    return await _counts(conn, _DOCUMENT_COUNTS)


async def job_counts(conn: AsyncConnection) -> dict[date, int]:
    return await _counts(conn, _JOB_COUNTS)


async def closure_counts(conn: AsyncConnection) -> dict[date, int]:
    return await _counts(conn, _CLOSURE_COUNTS)


async def documents(
    conn: AsyncConnection, day: date, *, after: int, chunk: int
) -> list[sa.Row]:
    """One keyset page of raw payloads fetched on `day`."""
    return list(
        await conn.execute(sa.text(_DOCUMENTS), {"day": day, "after": after, "chunk": chunk})
    )


async def jobs(conn: AsyncConnection, day: date, *, after: int, chunk: int) -> list[sa.Row]:
    """One keyset page of postings first seen on `day`, with their facets."""
    return list(
        await conn.execute(sa.text(_JOBS), {"day": day, "after": after, "chunk": chunk})
    )


async def closures(conn: AsyncConnection, day: date, *, after: int, chunk: int) -> list[sa.Row]:
    """One keyset page of postings retired on `day`."""
    return list(
        await conn.execute(sa.text(_CLOSURES), {"day": day, "after": after, "chunk": chunk})
    )
