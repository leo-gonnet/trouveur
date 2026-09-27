"""The one versioned work queue: "job N needs work of kind K, to reach version V".

It is what makes upgrade and backfill the same code path -- a version bump plus refill() enqueues
every row below it and the ordinary worker drains it, so the repair path stays tested.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.config import get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import freshness
from trouveur.db.schema import job, job_embedding, job_facet, work_item

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 60
# A claim older than this is assumed to belong to a worker that died mid-item.
STALE_CLAIM_AFTER = timedelta(minutes=30)


class WorkKind(StrEnum):
    DETAIL = "detail"
    DERIVE = "derive"
    EMBED = "embed"
    DEDUP = "dedup"


async def enqueue(
    conn: AsyncConnection, kind: WorkKind, job_ids: list[int], target_version: str
) -> int:
    """Queue work for specific jobs. Idempotent: the WHERE on the conflict clause is what stops
    a re-enqueue resetting an item's backoff and letting it spin."""
    if not job_ids:
        return 0
    stmt = pg_insert(work_item).values(
        [
            {"kind": kind.value, "job_id": job_id, "target_version": target_version}
            for job_id in job_ids
        ]
    )
    stmt = stmt.on_conflict_do_update(
        constraint="work_item_kind_job_uniq",
        set_={
            "target_version": stmt.excluded.target_version,
            "not_before": sa.func.now(),
            "claimed_at": None,
            "claimed_by": None,
            "attempts": 0,
            "last_error": None,
        },
        where=work_item.c.target_version.is_distinct_from(stmt.excluded.target_version),
    )
    return (await conn.execute(stmt)).rowcount or 0


async def claim(
    conn: AsyncConnection, kind: WorkKind, limit: int, worker: str
) -> list[sa.Row]:
    """Take up to `limit` ready items. SKIP LOCKED is required, not an optimisation: without it
    two workers on one kind serialise and the second does nothing."""
    ready = (
        sa.select(work_item.c.id)
        .where(
            work_item.c.kind == kind.value,
            work_item.c.claimed_at.is_(None),
            work_item.c.not_before <= sa.func.now(),
        )
        .order_by(work_item.c.not_before, work_item.c.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        work_item.update()
        .where(work_item.c.id.in_(ready))
        .values(claimed_at=sa.func.now(), claimed_by=worker)
        .returning(work_item.c.id, work_item.c.job_id, work_item.c.target_version)
    )
    return list(await conn.execute(stmt))


async def complete(conn: AsyncConnection, item_ids: list[int]) -> None:
    if not item_ids:
        return
    await conn.execute(work_item.delete().where(work_item.c.id.in_(item_ids)))


async def fail(conn: AsyncConnection, item_ids: list[int], error: str) -> None:
    """Return items to the queue with exponential backoff, or park them once exhausted. Parked
    items stay in the table: a dropped one is indistinguishable from work never needed."""
    if not item_ids:
        return
    backoff = sa.func.make_interval(
        0, 0, 0, 0, 0, 0,
        sa.cast(_BACKOFF_BASE_SECONDS * sa.func.pow(2, work_item.c.attempts), sa.Float),
    )
    await conn.execute(
        work_item.update()
        .where(work_item.c.id.in_(item_ids))
        .values(
            attempts=work_item.c.attempts + 1,
            last_error=error[:2000],
            claimed_at=None,
            claimed_by=None,
            not_before=sa.case(
                (
                    work_item.c.attempts + 1 >= MAX_ATTEMPTS,
                    sa.func.now() + sa.text("interval '30 days'"),
                ),
                else_=sa.func.now() + backoff,
            ),
        )
    )


async def pause_source(
    conn: AsyncConnection,
    kind: WorkKind,
    source: str,
    pause: timedelta,
    reason: str,
    claimed_ids: list[int],
) -> int:
    """Hold back every live item of one source for `pause`, spending no attempt.

    For a source failing wholesale -- 1,500 Workday detail fetches got a 403 in one burst, each
    burned all its attempts, and they parked while most of the postings closed. The fault was the
    source, not the posting, so the posting should not pay for it. Parked items are left alone,
    and so are items another worker holds; this worker's own claims in `claimed_ids` are released.
    """
    result = await conn.execute(
        work_item.update()
        .where(
            work_item.c.kind == kind.value,
            work_item.c.attempts < MAX_ATTEMPTS,
            work_item.c.job_id.in_(sa.select(job.c.id).where(job.c.source == source)),
            sa.or_(work_item.c.claimed_at.is_(None), work_item.c.id.in_(claimed_ids)),
        )
        .values(
            not_before=sa.func.greatest(work_item.c.not_before, sa.func.now() + pause),
            claimed_at=None,
            claimed_by=None,
            last_error=reason[:2000],
        )
    )
    return result.rowcount or 0


# The kinds closing a posting deletes (db/queries/ingest._DROP_CLOSED), so a requeue does not
# revive what a close would have dropped.
_DROPPED_ON_CLOSE = (WorkKind.DETAIL, WorkKind.EMBED)


async def requeue_parked(conn: AsyncConnection, kind: WorkKind) -> tuple[int, int]:
    """Give every parked item of `kind` a fresh set of attempts. Returns (requeued, dropped):
    an item whose posting has closed since it parked is deleted rather than retried."""
    parked = sa.and_(work_item.c.kind == kind.value, work_item.c.attempts >= MAX_ATTEMPTS)
    dropped = 0
    if kind in _DROPPED_ON_CLOSE:
        closed = sa.select(job.c.id).where(job.c.closed_at.isnot(None))
        result = await conn.execute(
            work_item.delete().where(parked, work_item.c.job_id.in_(closed))
        )
        dropped = result.rowcount or 0
    result = await conn.execute(
        work_item.update()
        .where(parked)
        .values(attempts=0, last_error=None, not_before=sa.func.now())
    )
    return result.rowcount or 0, dropped


async def release_stale(conn: AsyncConnection, older_than: timedelta = STALE_CLAIM_AFTER) -> int:
    cutoff = datetime.now(UTC) - older_than
    result = await conn.execute(
        work_item.update()
        .where(work_item.c.claimed_at.isnot(None), work_item.c.claimed_at < cutoff)
        .values(claimed_at=None, claimed_by=None)
    )
    return result.rowcount or 0


def _stale_query(
    kind: WorkKind, target_version: str, after_job_id: int, limit: int, fresh_since: datetime
):
    """Rows whose derived output is below `target_version`, in KEYSET order -- never OFFSET,
    which would re-scan everything it had already skipped on each chunk."""
    # Imported here: trouveur.db.queries reaches trouveur.ingest, which imports this module, so a
    # top-level import fails whenever trouveur.work is the first of them to load.
    from trouveur.db.queries import freshness

    if kind is WorkKind.DETAIL:
        # Only the source knows whether it has a detail phase, so refilling here would encode a
        # per-source fact in the queue -- exactly the leak the registry prevents.
        raise ValueError(
            "Detail work is enqueued at ingest by the source that needs it and is never refilled."
        )
    if kind is WorkKind.DERIVE:
        return (
            sa.select(job_facet.c.job_id)
            .where(
                job_facet.c.derive_version < int(target_version),
                job_facet.c.job_id > after_job_id,
            )
            .order_by(job_facet.c.job_id)
            .limit(limit)
        )
    if kind is WorkKind.DEDUP:
        return (
            sa.select(job.c.id.label("job_id"))
            .where(job.c.dedup_version < int(target_version), job.c.id > after_job_id)
            .order_by(job.c.id)
            .limit(limit)
        )
    # Open AND recent. Without the horizon here, every posting the pruner drops as stale is found
    # again by this query and re-embedded -- a loop that never terminates.
    return (
        sa.select(job.c.id.label("job_id"))
        .select_from(job.outerjoin(job_embedding, job_embedding.c.job_id == job.c.id))
        .where(
            job.c.closed_at.is_(None),
            freshness.clause(fresh_since),
            job.c.id > after_job_id,
            sa.or_(
                job_embedding.c.job_id.is_(None),
                job_embedding.c.embedding_version != target_version,
            ),
        )
        .order_by(job.c.id)
        .limit(limit)
    )


async def refill(
    conn: AsyncConnection,
    kind: WorkKind,
    target_version: str,
    *,
    chunk_size: int = 5000,
    after_job_id: int = 0,
) -> tuple[int, int | None]:
    """Enqueue one chunk of out-of-date rows. Returns (enqueued, cursor for the next chunk).

    Chunked and resumable, so stopping halfway and re-running are both free.
    """
    from trouveur.db.queries import freshness

    fresh_since = freshness.fresh_since(get_settings().retrieval_horizon_days)
    rows = list(
        await conn.execute(
            _stale_query(kind, target_version, after_job_id, chunk_size, fresh_since)
        )
    )
    if not rows:
        return 0, None
    job_ids = [row.job_id for row in rows]
    enqueued = await enqueue(conn, kind, job_ids, target_version)
    return enqueued, job_ids[-1]


async def refill_all(
    kind: WorkKind,
    target_version: str,
    *,
    chunk_size: int = 5000,
    on_chunk: Callable[[int, int], None] | None = None,
) -> int:
    """Refill to the end of the table, one transaction per chunk so a long refill never holds
    one open. Idempotent: a row already queued at this version is left as it is."""
    cursor, total = 0, 0
    while True:
        async with connect() as conn:
            queued, cursor = await refill(
                conn, kind, target_version, chunk_size=chunk_size, after_job_id=cursor
            )
        total += queued
        if cursor is None:
            return total
        if on_chunk is not None:
            on_chunk(total, cursor)


async def backlog(conn: AsyncConnection) -> dict[str, dict[str, int]]:
    """Queue depth per kind, for the dashboard. Parked items are counted separately."""
    rows = await conn.execute(
        sa.select(
            work_item.c.kind,
            sa.func.count().label("total"),
            sa.func.count().filter(work_item.c.attempts >= MAX_ATTEMPTS).label("parked"),
            sa.func.count().filter(work_item.c.claimed_at.isnot(None)).label("claimed"),
        ).group_by(work_item.c.kind)
    )
    return {
        row.kind: {"total": row.total, "parked": row.parked, "claimed": row.claimed}
        for row in rows
    }
