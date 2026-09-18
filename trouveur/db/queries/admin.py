"""Operational SQL: the board registry, the run queue, the schedule and the health panels."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.schema import (
    job,
    pipeline_run,
    pipeline_schedule,
    source_scope_health,
    source_sweep,
    source_tenant,
    work_item,
)
from trouveur.models import RunStatus, RunTrigger, TenantOrigin


async def enabled_tenants(conn: AsyncConnection) -> dict[str, list[str]]:
    """The crawl set, grouped by source, so the pipeline loads every source's tenants at once.

    Grouped rather than fetched per source on purpose: the pipeline must not have to know which
    sources are tenant-scoped, or adding one becomes an edit there as well as in the registry.
    """
    rows = await conn.execute(
        sa.select(source_tenant.c.source, source_tenant.c.scope)
        .where(source_tenant.c.enabled.is_(True))
        .order_by(source_tenant.c.source, source_tenant.c.scope)
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row.source, []).append(row.scope)
    return grouped


async def list_tenants(conn: AsyncConnection, source: str | None = None) -> list[sa.Row]:
    """Every tenant with its health, including candidates a discovery pass has proposed."""
    stmt = (
        sa.select(
            source_tenant.c.source,
            source_tenant.c.scope,
            source_tenant.c.enabled,
            source_tenant.c.origin,
            source_tenant.c.note,
            source_tenant.c.added_at,
            source_scope_health.c.last_ok_at,
            source_scope_health.c.last_documents,
            source_scope_health.c.last_error,
            source_scope_health.c.consecutive_failures,
        )
        .select_from(
            source_tenant.outerjoin(
                source_scope_health,
                sa.and_(
                    source_scope_health.c.source == source_tenant.c.source,
                    source_scope_health.c.scope == source_tenant.c.scope,
                ),
            )
        )
        .order_by(source_tenant.c.source, source_tenant.c.scope)
    )
    if source:
        stmt = stmt.where(source_tenant.c.source == source)
    return list(await conn.execute(stmt))


async def add_tenants(
    conn: AsyncConnection,
    source: str,
    scopes: Sequence[str],
    *,
    enabled: bool = True,
    origin: TenantOrigin = TenantOrigin.MANUAL,
    note: str | None = None,
) -> int:
    """Register tenants. Idempotent, and never re-enables one an operator has switched off."""
    if not scopes:
        return 0
    stmt = pg_insert(source_tenant).values(
        [
            {
                "source": source,
                "scope": scope,
                "enabled": enabled,
                "origin": TenantOrigin(origin).value,
                "note": note,
            }
            for scope in scopes
        ]
    )
    result = await conn.execute(
        stmt.on_conflict_do_nothing(
            index_elements=[source_tenant.c.source, source_tenant.c.scope]
        )
    )
    return result.rowcount or 0


async def set_tenant_enabled(
    conn: AsyncConnection, source: str, scope: str, enabled: bool
) -> None:
    await conn.execute(
        source_tenant.update()
        .where(source_tenant.c.source == source, source_tenant.c.scope == scope)
        .values(enabled=enabled)
    )


async def remove_tenant(conn: AsyncConnection, source: str, scope: str) -> None:
    await conn.execute(
        source_tenant.delete().where(
            source_tenant.c.source == source, source_tenant.c.scope == scope
        )
    )
    await conn.execute(
        source_scope_health.delete().where(
            source_scope_health.c.source == source, source_scope_health.c.scope == scope
        )
    )


async def record_scope_health(
    conn: AsyncConnection, source: str, results: Sequence[Any]
) -> None:
    """Persist what each tenant did this sweep.

    Called on every sweep, win or lose. Its predecessor was never called at all, so the health
    columns on the old board table were permanently empty and the page that displayed them showed
    "last success: —" forever, which reads as "not run yet" rather than "not recorded".
    """
    if not results:
        return
    rows = [
        {
            "source": source,
            "scope": result.scope,
            "last_ok_at": (sa.func.now() if result.ok else None),
            "last_documents": result.documents,
            "last_error": (result.error or "")[:2000] or None,
            "consecutive_failures": 0 if result.ok else 1,
        }
        for result in results
    ]
    stmt = pg_insert(source_scope_health).values(rows)
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[source_scope_health.c.source, source_scope_health.c.scope],
            set_={
                # A success resets the streak; a failure extends whatever was already there, so a
                # slug that has been 404ing for a week is visibly different from one that blipped.
                "last_ok_at": sa.func.coalesce(
                    stmt.excluded.last_ok_at, source_scope_health.c.last_ok_at
                ),
                "last_documents": stmt.excluded.last_documents,
                "last_error": stmt.excluded.last_error,
                "consecutive_failures": sa.case(
                    (stmt.excluded.consecutive_failures == 0, 0),
                    else_=source_scope_health.c.consecutive_failures + 1,
                ),
                "updated_at": sa.func.now(),
            },
        )
    )


async def scope_health(conn: AsyncConnection, failing_only: bool = False) -> list[sa.Row]:
    """Per-tenant health, worst first.

    A tenant that has been failing for days is a line to remove from the registry file in the
    repository, so this is the panel that drives an actual code change rather than a UI edit.
    """
    stmt = source_scope_health.select()
    if failing_only:
        stmt = stmt.where(source_scope_health.c.consecutive_failures > 0)
    return list(
        await conn.execute(
            stmt.order_by(
                source_scope_health.c.consecutive_failures.desc(),
                source_scope_health.c.source,
                source_scope_health.c.scope,
            )
        )
    )


async def prune_scope_health(
    conn: AsyncConnection, source: str, known_scopes: Sequence[str]
) -> int:
    """Forget tenants no longer registered, so deregistering one also clears its health row."""
    result = await conn.execute(
        source_scope_health.delete().where(
            source_scope_health.c.source == source,
            source_scope_health.c.scope.notin_(list(known_scopes)),
        )
    )
    return result.rowcount or 0


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


async def description_coverage(conn: AsyncConnection) -> int:
    """Open postings that actually have a description.

    A posting with only a title embeds on far less text, so the share of the corpus that has one
    changes what any retrieval measurement means.
    """
    return int(
        (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(job)
                .where(job.c.closed_at.is_(None), job.c.description.isnot(None))
            )
        ).scalar_one()
        or 0
    )


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
    conn: AsyncConnection, *, trigger: RunTrigger, only_source: str | None = None,
    backfill: bool = False,
) -> int:
    return (
        await conn.execute(
            pipeline_run.insert()
            .values(
                trigger=RunTrigger(trigger).value,
                only_source=only_source,
                backfill=backfill,
            )
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
            pipeline_run.c.status == RunStatus.QUEUED.value,
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
                status=RunStatus.RUNNING.value,
                started_at=sa.func.now(),
                attempts=pipeline_run.c.attempts + 1,
            )
            .returning(pipeline_run)
        )
    ).one_or_none()


async def finish_run(
    conn: AsyncConnection, run_id: int, *, status: RunStatus, report: dict | None = None,
    error: str | None = None,
) -> None:
    await conn.execute(
        pipeline_run.update()
        .where(pipeline_run.c.id == run_id)
        .values(
            status=RunStatus(status).value, finished_at=sa.func.now(), report=report,
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
            .where(pipeline_run.c.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]))
            .order_by(pipeline_run.c.queued_at)
            .limit(1)
        )
    ).one_or_none()


async def request_cancel(conn: AsyncConnection, run_id: int) -> bool:
    """Ask a run to stop. Only a flag: the runner decides when it is safe to act on it.

    A queued run has not started, so it is cancelled outright. A running one is left to the runner,
    which stops between sources -- killing it mid-source would leave a partially observed source
    that must never be treated as complete.
    """
    cancelled_now = await conn.execute(
        pipeline_run.update()
        .where(
            pipeline_run.c.id == run_id,
            pipeline_run.c.status == RunStatus.QUEUED.value,
        )
        .values(
            status=RunStatus.CANCELLED.value,
            finished_at=sa.func.now(),
            cancel_requested=True,
        )
    )
    if cancelled_now.rowcount:
        return True
    asked = await conn.execute(
        pipeline_run.update()
        .where(
            pipeline_run.c.id == run_id,
            pipeline_run.c.status == RunStatus.RUNNING.value,
        )
        .values(cancel_requested=True)
    )
    return bool(asked.rowcount)


async def cancel_requested(conn: AsyncConnection, run_id: int) -> bool:
    return bool(
        await conn.scalar(
            sa.select(pipeline_run.c.cancel_requested).where(pipeline_run.c.id == run_id)
        )
    )


async def start_run_progress(conn: AsyncConnection, run_id: int, sources_total: int) -> None:
    await conn.execute(
        pipeline_run.update()
        .where(pipeline_run.c.id == run_id)
        .values(sources_total=sources_total, sources_done=0, current_source=None)
    )


async def advance_run_progress(
    conn: AsyncConnection, run_id: int, *, done: int, current_source: str | None
) -> None:
    await conn.execute(
        pipeline_run.update()
        .where(pipeline_run.c.id == run_id)
        .values(sources_done=done, current_source=current_source)
    )


async def running_sweeps(conn: AsyncConnection) -> list[sa.Row]:
    """Sweeps with no verdict yet -- the ones happening right now.

    `start_sweep` inserts the row before the first request, so a source appears here the moment it
    begins and its documents_seen climbs while it works.
    """
    return list(
        await conn.execute(
            sa.select(
                source_sweep.c.source,
                source_sweep.c.started_at,
                source_sweep.c.documents_seen,
            )
            .where(source_sweep.c.ok.is_(None))
            .order_by(source_sweep.c.started_at)
        )
    )


async def typical_sweep_seconds(conn: AsyncConnection, limit: int = 5) -> dict[str, float]:
    """Median wall-clock seconds per source over its last few finished sweeps.

    The only defensible basis for an estimate: how long this source actually took here, on this
    crawl set. A source with no history is absent from the result rather than guessed at, so the
    page can say it does not know instead of inventing a number.
    """
    rows = await conn.execute(
        sa.text(
            """
            SELECT source,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))
                   ) AS seconds
            FROM (
                SELECT source, started_at, finished_at,
                       row_number() OVER (PARTITION BY source ORDER BY started_at DESC) AS rn
                FROM source_sweep
                WHERE finished_at IS NOT NULL AND ok
            ) recent
            WHERE rn <= :limit
            GROUP BY source
            """
        ),
        {"limit": limit},
    )
    return {row.source: float(row.seconds or 0) for row in rows}


async def fail_orphaned_runs(conn: AsyncConnection) -> int:
    """Fail runs left 'running' by a process that died. Without this the queue wedges."""
    result = await conn.execute(
        pipeline_run.update()
        .where(
            pipeline_run.c.status == RunStatus.RUNNING.value,
            pipeline_run.c.started_at < sa.func.now() - sa.text("interval '6 hours'"),
        )
        .values(
            status=RunStatus.FAILED.value,
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


