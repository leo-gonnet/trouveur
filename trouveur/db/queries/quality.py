"""SQL for the Quality page: what the scorer said each night, and the nightly recall checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.schema import recall_check, recall_needle

# A needle is a job the reader kept -- saved it or applied to it -- for as long as they keep it,
# open or closed, new or old. A reader keeps a few jobs a month, all at once: dropping them as
# they age would leave the check with nothing to measure most nights.
_NEEDLES = """
SELECT job_id
FROM user_job_match
WHERE user_id = :user_id AND state IN ('saved', 'applied')
ORDER BY job_id
"""


async def needles(conn: AsyncConnection, user_id: int) -> list[int]:
    rows = await conn.execute(sa.text(_NEEDLES), {"user_id": user_id})
    return [row.job_id for row in rows]


# What a vector is built from, for jobs of any age and state: the stored one is deleted when a
# posting closes or leaves the horizon, so the check builds its own.
_NEEDLE_TEXTS = """
SELECT id, title, company, description, locations
FROM job
WHERE id = ANY(CAST(:job_ids AS bigint[]))
ORDER BY id
"""


async def needle_texts(conn: AsyncConnection, job_ids: Sequence[int]) -> list[sa.Row]:
    if not job_ids:
        return []
    return list(await conn.execute(sa.text(_NEEDLE_TEXTS), {"job_ids": list(job_ids)}))


async def record_check(
    conn: AsyncConnection, user_id: int, profile_version: int, ranks: Mapping[int, int | None]
) -> int:
    check_id = (
        await conn.execute(
            recall_check.insert()
            .values(user_id=user_id, profile_version=profile_version)
            .returning(recall_check.c.id)
        )
    ).scalar_one()
    if ranks:
        await conn.execute(
            recall_needle.insert(),
            [{"check_id": check_id, "job_id": job, "rank": rank} for job, rank in ranks.items()],
        )
    return check_id


# One row per needle per check, plus one with no job for a check that had no needles, so a night
# with nothing to look for still appears as a night.
_CHECKS = """
SELECT c.id AS check_id, c.checked_at, c.profile_version, n.job_id, n.rank
FROM recall_check c
LEFT JOIN recall_needle n ON n.check_id = c.id
WHERE c.user_id = :user_id AND c.checked_at >= :since
ORDER BY c.checked_at, n.job_id
"""


async def checks(conn: AsyncConnection, user_id: int, since: datetime) -> list[sa.Row]:
    return list(await conn.execute(sa.text(_CHECKS), {"user_id": user_id, "since": since}))


# What became of a job since: enough to say why it stopped being a needle.
_NEEDLE_JOBS = """
SELECT j.id AS job_id, j.public_id, j.title, j.company, m.state::text AS state
FROM job j
LEFT JOIN user_job_match m ON m.job_id = j.id AND m.user_id = :user_id
WHERE j.id = ANY(CAST(:job_ids AS bigint[]))
"""


async def needle_jobs(
    conn: AsyncConnection, user_id: int, job_ids: Sequence[int]
) -> list[sa.Row]:
    if not job_ids:
        return []
    return list(
        await conn.execute(
            sa.text(_NEEDLE_JOBS), {"user_id": user_id, "job_ids": list(job_ids)}
        )
    )


# Every posting scored for this reader in the window, with what the reader did with it. The
# current verdict only: a re-score under a new profile replaces the row, which is the score the
# reader was last shown.
_SCORES = """
SELECT job_id, llm_score, scored_at, state::text AS state
FROM user_job_match
WHERE user_id = :user_id AND llm_score IS NOT NULL AND scored_at >= :since
ORDER BY scored_at
"""


async def scores(conn: AsyncConnection, user_id: int, since: datetime) -> list[sa.Row]:
    return list(await conn.execute(sa.text(_SCORES), {"user_id": user_id, "since": since}))
