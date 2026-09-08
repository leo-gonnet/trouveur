"""The versioned work queue.

One queue serves every deferred stage. A row here means "job N needs work of kind K, to reach
version V", which is simultaneously the backlog and the change notification -- so there is no
separate outbox to keep in step with it.

The queue is what makes an upgrade indistinguishable from a backfill. Bumping DERIVE_VERSION and
calling refill() enqueues every row below the new version; the same worker that filled the table
initially drains it. That is the only arrangement in which the upgrade path stays tested, because
it is exercised on every ordinary run.

Every operation here is batched. Per-row round trips are what actually bottleneck a pipeline at
this size, not the expensive work the rows describe.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

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
    """Queue work for specific jobs. Idempotent: re-queueing at the same version is a no-op.

    The WHERE on the conflict clause is what makes a re-run free. Without it, re-enqueueing an
    already-queued item would reset its backoff and it could spin.
    """
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
    """Take up to `limit` ready items.

    SKIP LOCKED is required, not an optimisation: without it two workers draining the same kind
    serialise behind each other's row locks and the second one does nothing.
    """
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
    """Return items to the queue with exponential backoff, or park them once exhausted.

    Parked items are left in the table on purpose: a silently dropped item is indistinguishable
    from work that was never needed, and the dashboard counts these.
    """
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


async def release_stale(conn: AsyncConnection, older_than: timedelta = STALE_CLAIM_AFTER) -> int:
    cutoff = datetime.now(UTC) - older_than
    result = await conn.execute(
        work_item.update()
        .where(work_item.c.claimed_at.isnot(None), work_item.c.claimed_at < cutoff)
        .values(claimed_at=None, claimed_by=None)
    )
    return result.rowcount or 0


def _stale_query(kind: WorkKind, target_version: str, after_job_id: int, limit: int):
    """Rows whose derived output is below `target_version`, in keyset order.

    Keyset, not OFFSET: this is the query that runs over the whole corpus after a version bump,
    and OFFSET would re-scan everything it had already skipped on each successive chunk.
    """
    if kind is WorkKind.DETAIL:
        # Detail work is enqueued by the sweep that discovered the posting, because only the
        # source knows whether it has a detail phase at all. Refilling it here would mean
        # encoding that per-source fact in the queue, which is exactly the leak the registry
        # exists to prevent. A detail that will not fetch is parked and visible, not re-derived.
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
    # Embeddings exist for open postings only, so a missing row is work and a closed job is not.
    return (
        sa.select(job.c.id.label("job_id"))
        .select_from(job.outerjoin(job_embedding, job_embedding.c.job_id == job.c.id))
        .where(
            job.c.closed_at.is_(None),
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

    Chunked and resumable so that stopping halfway is free and re-running is free: a long refill
    over millions of rows must be something you can interrupt without a second thought, not a job
    you are afraid to touch once it has started.
    """
    rows = list(await conn.execute(_stale_query(kind, target_version, after_job_id, chunk_size)))
    if not rows:
        return 0, None
    job_ids = [row.job_id for row in rows]
    enqueued = await enqueue(conn, kind, job_ids, target_version)
    return enqueued, job_ids[-1]


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
