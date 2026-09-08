"""Operational SQL: the board registry, the run queue, the schedule and the health panels."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.schema import (
    greenhouse_board,
    job,
    pipeline_run,
    pipeline_schedule,
    source_sweep,
    work_item,
)


async def enabled_greenhouse_boards(conn: AsyncConnection) -> list[str]:
    rows = await conn.execute(
        sa.select(greenhouse_board.c.slug)
        .where(greenhouse_board.c.enabled.is_(True))
        .order_by(greenhouse_board.c.slug)
    )
    return [row.slug for row in rows]


async def list_greenhouse_boards(conn: AsyncConnection) -> list[sa.Row]:
    return list(
        await conn.execute(greenhouse_board.select().order_by(greenhouse_board.c.slug))
    )


async def add_greenhouse_boards(conn: AsyncConnection, slugs: Sequence[str]) -> int:
    if not slugs:
        return 0
    stmt = pg_insert(greenhouse_board).values([{"slug": slug} for slug in slugs])
    result = await conn.execute(
        stmt.on_conflict_do_nothing(index_elements=[greenhouse_board.c.slug])
    )
    return result.rowcount or 0


async def set_greenhouse_board_enabled(
    conn: AsyncConnection, slug: str, enabled: bool
) -> None:
    await conn.execute(
        greenhouse_board.update()
        .where(greenhouse_board.c.slug == slug)
        .values(enabled=enabled)
    )


async def delete_greenhouse_board(conn: AsyncConnection, slug: str) -> None:
    await conn.execute(greenhouse_board.delete().where(greenhouse_board.c.slug == slug))


async def record_board_result(
    conn: AsyncConnection, slug: str, *, ok: bool, error: str | None = None
) -> None:
    values: dict[str, Any] = (
        {"last_ok_at": sa.func.now(), "last_error": None, "consecutive_failures": 0}
        if ok
        else {
            "last_error": (error or "")[:2000],
            "consecutive_failures": greenhouse_board.c.consecutive_failures + 1,
        }
    )
    await conn.execute(
        greenhouse_board.update().where(greenhouse_board.c.slug == slug).values(**values)
    )


async def recent_sweeps(conn: AsyncConnection, limit: int = 20) -> list[sa.Row]:
    return list(
        await conn.execute(
            source_sweep.select().order_by(source_sweep.c.started_at.desc()).limit(limit)
        )
    )


async def source_health(conn: AsyncConnection) -> list[sa.Row]:
    """Latest sweep per source.

    Surfaces completeness and partition overflow, not just success: a sweep that ran fine while
    silently reaching only the first 10000 rows of a partition is the failure mode worth seeing,
    and it looks identical to a healthy one in any count of documents collected.
    """
    latest = (
        sa.select(
            source_sweep.c.source,
            sa.func.max(source_sweep.c.started_at).label("started_at"),
        )
        .group_by(source_sweep.c.source)
        .subquery()
    )
    return list(
        await conn.execute(
            sa.select(source_sweep)
            .join(
                latest,
                sa.and_(
                    source_sweep.c.source == latest.c.source,
                    source_sweep.c.started_at == latest.c.started_at,
                ),
            )
            .order_by(source_sweep.c.source)
        )
    )


async def corpus_overview(conn: AsyncConnection) -> sa.Row:
    return (
        await conn.execute(
            sa.select(
                sa.func.count().label("total"),
                sa.func.count().filter(job.c.closed_at.is_(None)).label("open"),
                sa.func.count()
                .filter(job.c.first_seen_at > sa.func.now() - sa.text("interval '1 day'"))
                .label("new_today"),
                sa.func.count(sa.distinct(job.c.company)).label("companies"),
            ).select_from(job)
        )
    ).one()


async def derived_coverage(conn: AsyncConnection) -> sa.Row:
    """How much of the open corpus is actually usable by retrieval.

    Facets and embeddings arrive asynchronously, so a job can be stored, searchable and still
    invisible to the recommender. Counting them separately makes that gap visible instead of
    looking like poor recall.
    """
    return (
        await conn.execute(
            sa.text(
                """
                SELECT
                    (SELECT count(*) FROM job WHERE closed_at IS NULL) AS open_jobs,
                    (SELECT count(*) FROM job j JOIN job_facet f ON f.job_id = j.id
                     WHERE j.closed_at IS NULL AND f.derive_version > 0) AS derived,
                    (SELECT count(*) FROM job_embedding) AS embedded,
                    (SELECT count(DISTINCT embedding_version) FROM job_embedding) AS vector_spaces
                """
            )
        )
    ).one()


async def ensure_schedule(conn: AsyncConnection) -> None:
    await conn.execute(
        pg_insert(pipeline_schedule)
        .values(id=1)
        .on_conflict_do_nothing(index_elements=[pipeline_schedule.c.id])
    )


async def get_schedule(conn: AsyncConnection) -> sa.Row:
    await ensure_schedule(conn)
    return (
        await conn.execute(pipeline_schedule.select().where(pipeline_schedule.c.id == 1))
    ).one()


async def update_schedule(conn: AsyncConnection, values: dict[str, Any]) -> None:
    await conn.execute(
        pipeline_schedule.update()
        .where(pipeline_schedule.c.id == 1)
        .values(**values, updated_at=sa.func.now())
    )


async def enqueue_run(
    conn: AsyncConnection, *, trigger: str, only_source: str | None = None,
    backfill: bool = False,
) -> int:
    return (
        await conn.execute(
            pipeline_run.insert()
            .values(trigger=trigger, only_source=only_source, backfill=backfill)
            .returning(pipeline_run.c.id)
        )
    ).scalar_one()


async def claim_next_run(conn: AsyncConnection) -> sa.Row | None:
    """Take the next queued run.

    SKIP LOCKED so a second runner cannot pick up the same row, and a bounded attempts column so a
    run that keeps crashing eventually stops being retried rather than looping forever.
    """
    ready = (
        sa.select(pipeline_run.c.id)
        .where(
            pipeline_run.c.status == "queued",
            pipeline_run.c.not_before <= sa.func.now(),
        )
        .order_by(pipeline_run.c.not_before, pipeline_run.c.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return (
        await conn.execute(
            pipeline_run.update()
            .where(pipeline_run.c.id.in_(ready))
            .values(
                status="running",
                started_at=sa.func.now(),
                attempts=pipeline_run.c.attempts + 1,
            )
            .returning(pipeline_run)
        )
    ).one_or_none()


async def finish_run(
    conn: AsyncConnection, run_id: int, *, status: str, report: dict | None = None,
    error: str | None = None,
) -> None:
    await conn.execute(
        pipeline_run.update()
        .where(pipeline_run.c.id == run_id)
        .values(
            status=status, finished_at=sa.func.now(), report=report,
            error=(error or "")[:4000] or None,
        )
    )


async def recent_runs(conn: AsyncConnection, limit: int = 20) -> list[sa.Row]:
    return list(
        await conn.execute(
            pipeline_run.select().order_by(pipeline_run.c.queued_at.desc()).limit(limit)
        )
    )


async def active_run(conn: AsyncConnection) -> sa.Row | None:
    return (
        await conn.execute(
            pipeline_run.select()
            .where(pipeline_run.c.status.in_(["queued", "running"]))
            .order_by(pipeline_run.c.queued_at)
            .limit(1)
        )
    ).one_or_none()


async def fail_orphaned_runs(conn: AsyncConnection) -> int:
    """Fail runs left 'running' by a process that died. Without this the queue wedges."""
    result = await conn.execute(
        pipeline_run.update()
        .where(
            pipeline_run.c.status == "running",
            pipeline_run.c.started_at < sa.func.now() - sa.text("interval '6 hours'"),
        )
        .values(
            status="failed",
            finished_at=sa.func.now(),
            error="The runner process disappeared while this run was in progress.",
        )
    )
    return result.rowcount or 0


async def facet_breakdown(conn: AsyncConnection, limit: int = 12) -> list[sa.Row]:
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT country, count(*) AS jobs
                FROM job j
                JOIN job_facet f ON f.job_id = j.id, unnest(f.countries) AS country
                WHERE j.closed_at IS NULL
                GROUP BY country
                ORDER BY jobs DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    )


async def queue_depth(conn: AsyncConnection) -> list[sa.Row]:
    return list(
        await conn.execute(
            sa.select(
                work_item.c.kind,
                sa.func.count().label("total"),
                sa.func.min(work_item.c.created_at).label("oldest"),
            ).group_by(work_item.c.kind)
        )
    )


