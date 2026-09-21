"""SQL for the offsite corpus export.

Reads only, and reads in arrival order: `source_document` by `fetched_at`, `job` by
`first_seen_at`, closures by `closed_at`. That order is the whole design. A day of the archive is
a contiguous slice that never changes again, so a day is a file, a file is written once, and
"which days are already uploaded" is the only state the export needs -- which means it can be read
back from the destination instead of being tracked here and drifting.

Enums, UUIDs, JSONB and bytea are cast to text in the query rather than converted in Python. The
archive outlives this schema: a Parquet file holding `'listing'` and a hex digest is readable in
ten years by something that has never heard of `document_kind`, and a driver-level type mapping
is exactly the kind of thing that changes underneath a format that is supposed to be stable.
"""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

# Deliberately absent from every projection below:
#
#   search_de, search_fold   generated search columns; a pure function of the text beside them.
#   job_embedding            vectors, recomputable from the text and worthless in another model's
#                            space. The archive exists so the corpus survives a model change, not
#                            so one model's output does.
#   user_*, llm_score_cache  one person's account, credentials and match history. The corpus is
#                            public job postings; none of this is, and a private repository is not
#                            a reason to upload it.

# How many rows each day holds, for every day the stream has any. One aggregate per stream
# rather than a probe per day: it answers "which days exist" and "how big is each" at once, so a
# day that never had anything in it is simply absent instead of being re-checked every night for
# the rest of the installation's life. It is also what makes --dry-run worth running, since the
# alternative is a dry run that reports every partition as zero rows.
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

# Closures are the one thing a day partition of `job` cannot carry: a posting ingested in March
# and retired in September changes a file that was written six months earlier. So retirement is
# its own append-only stream, keyed by the day it happened, and a reader reconstructs state at
# any date by replaying it over the job partitions.
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
