"""SQL for a reader's "I found this job somewhere else": the queue, the lookup, the answer.

The lookup is the only interesting part. A reader pastes the link they were looking at, which is
rarely the link we stored for the same posting -- the public board site rather than the API host,
the aggregator's copy, a tracking parameter from wherever they found it. Two statements answer it,
cheapest first:

1. the posting's own id at the source, inside the board the link resolved to. Scoped that tightly
   the id cannot collide, and it rides the `(source, external_id)` unique index.
2. the URL itself, against a handful of spellings of the pasted one. This one is a scan of `job`,
   which is why it runs second and only when the first found nothing. A few reports a day make
   that cheaper than an index on a column nothing else queries.

A miss is the expensive mistake, not a scan: it tells a reader we never collected a posting we did
collect, and files a board we already sweep as a gap.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.queries import location
from trouveur.db.schema import user_report


async def create(conn: AsyncConnection, *, user_id: int, url: str) -> bool:
    """File one report, unanswered. True when it was written.

    DO NOTHING on the reader's still-open questions: a double-pressed button, or the same link
    pasted twice while the first is waiting, is one question. Once answered the same link can be
    asked again -- the corpus moves, and the new answer stands beside the old one.
    """
    stmt = pg_insert(user_report).values(user_id=user_id, url=url)
    result = await conn.execute(
        stmt.on_conflict_do_nothing(
            index_elements=[user_report.c.user_id, user_report.c.url],
            index_where=user_report.c.outcome.is_(None),
        )
    )
    return bool(result.rowcount)


_PENDING = """
SELECT id, user_id, url
FROM user_report
WHERE outcome IS NULL
ORDER BY id
LIMIT :chunk
"""


async def pending(conn: AsyncConnection, *, chunk: int) -> list[sa.Row]:
    """The reports we still owe an answer for, oldest first. A reader is waiting on each one."""
    return list(await conn.execute(sa.text(_PENDING), {"chunk": chunk}))


# Our copy of the posting, by its id at the source. `scope` is NULL for a source that sweeps one
# global corpus, and then the source alone is the whole constraint.
_BY_EXTERNAL_ID = """
SELECT id, public_id
FROM job
WHERE source = CAST(:source AS text)
  AND external_id = ANY(CAST(:ids AS text[]))
  AND (CAST(:scope AS text) IS NULL OR scope = CAST(:scope AS text))
ORDER BY closed_at ASC NULLS FIRST, id
LIMIT 1
"""

# The same posting by URL, against every spelling of the pasted link worth trying. Open first:
# a board that re-posts a role leaves us both, and the one the reader can still apply to is the
# one they asked about.
_BY_URL = """
SELECT id, public_id
FROM job
WHERE url = ANY(CAST(:urls AS text[]))
ORDER BY closed_at ASC NULLS FIRST, id
LIMIT 1
"""


async def find_job(
    conn: AsyncConnection,
    *,
    urls: Sequence[str],
    source: str | None,
    scope: str | None,
    ids: Sequence[str],
) -> sa.Row | None:
    """Our copy of the posting a link points at, or None."""
    if source and ids:
        found = (
            await conn.execute(
                sa.text(_BY_EXTERNAL_ID),
                {"source": source, "ids": list(ids), "scope": scope},
            )
        ).one_or_none()
        if found is not None:
            return found
    if not urls:
        return None
    return (
        await conn.execute(sa.text(_BY_URL), {"urls": list(urls)})
    ).one_or_none()


# Why a posting we hold did or did not reach this reader. Three facts, in the order the pipeline
# applies them, so the answer can name the FIRST stage that stopped it.
#
# `job_facet` is joined LEFT and the filter defaults to passing: a posting still waiting to be
# derived cannot be judged on its location, and what stopped it was the next stage, not the
# filter. Same direction as the filter itself -- an unplaced posting passes.
_VERDICT = f"""
SELECT
    EXISTS (
        SELECT 1 FROM user_edition_item e WHERE e.user_id = :user_id AND e.job_id = j.id
    ) AS shown,
    m.user_id IS NOT NULL AS retrieved,
    COALESCE({location.SQL}, TRUE) AS in_area
FROM job j
LEFT JOIN job_facet f ON f.job_id = j.id
LEFT JOIN user_job_match m ON m.user_id = :user_id AND m.job_id = j.id
WHERE j.id = :job_id
"""


async def verdict(conn: AsyncConnection, *, user_id: int, job_id: int, profile: Any) -> sa.Row:
    """What happened to one posting for one reader. `profile` is whatever the filter reads."""
    return (
        await conn.execute(
            sa.text(_VERDICT),
            {"user_id": user_id, "job_id": job_id, **location.params(profile)},
        )
    ).one()


async def answer(
    conn: AsyncConnection,
    *,
    report_id: int,
    outcome: str,
    reason: str | None,
    job_id: int | None,
    source: str | None,
    scope: str | None,
) -> None:
    """Store the answer. Written once; the WHERE clause is what keeps it that way."""
    await conn.execute(
        user_report.update()
        .where(user_report.c.id == report_id, user_report.c.outcome.is_(None))
        .values(
            outcome=outcome,
            reason=reason,
            job_id=job_id,
            source=source,
            scope=scope,
            answered_at=sa.func.now(),
        )
    )


_FOR_USER = """
SELECT r.id, r.url, r.outcome, r.reason, r.source, r.scope, r.created_at, r.answered_at,
       j.public_id, j.title, j.company, j.closed_at
FROM user_report r
LEFT JOIN job j ON j.id = r.job_id
WHERE r.user_id = :user_id
ORDER BY r.id DESC
LIMIT :chunk
"""


async def for_user(conn: AsyncConnection, user_id: int, *, chunk: int) -> list[sa.Row]:
    """One reader's own reports, newest first, with our copy of each posting where there is one."""
    return list(await conn.execute(sa.text(_FOR_USER), {"user_id": user_id, "chunk": chunk}))
