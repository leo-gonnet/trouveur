"""The runner: the only process that sweeps sources and drains queues.

Each tick does queue work FIRST and run work second: draining is what makes yesterday's sweep
usable, and a runner that only worked during a scan would leave the corpus a step behind itself.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import UTC, datetime

from trouveur import clock
from trouveur.config import Settings, get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.ingest import pipeline as ingest
from trouveur.ingest import workers
from trouveur.match import pipeline as matching
from trouveur.models import RunStatus, RunTrigger
from trouveur.notify import send_digests
from trouveur.sources import build_sources
from trouveur.sources.http import PoliteClient
from trouveur.work import release_stale

log = logging.getLogger(__name__)

TICK_SECONDS = 20
_DETAIL_PER_TICK = 40


def slot_today(schedule, now: datetime) -> datetime:
    """The instant today's slot falls at, from the wall-clock hour the operator typed.

    Resolved in the installation's timezone rather than in UTC: `run_hour = 7` is "seven in the
    morning", and computed on a UTC clock it drifts to eight when the clocks change -- twice a
    year, silently, on a page whose only label is "Hour".

    On the spring-forward day an hour that does not exist locally resolves to the instant after
    the gap. The slot still passes exactly once, which is all this has to guarantee.
    """
    local = clock.to_local(now).replace(
        hour=schedule.run_hour, minute=schedule.run_minute, second=0, microsecond=0
    )
    return local.astimezone(UTC)


def is_scheduled_run_due(schedule, now: datetime, last_queued_at: datetime | None) -> bool:
    """Whether today's slot has passed without a scheduled run being queued for it.

    Compared against the slot rather than against an interval, so a runner that was down over its
    slot still runs once when it comes back, and does not then run repeatedly to "catch up".
    """
    if not schedule.enabled:
        return False
    slot = slot_today(schedule, now)
    if now < slot:
        return False
    return last_queued_at is None or last_queued_at < slot


async def _maybe_enqueue_scheduled(conn) -> None:
    schedule = await admin_q.get_schedule(conn)
    runs = await admin_q.recent_runs(conn, limit=50)
    scheduled = [run for run in runs if run.trigger == RunTrigger.SCHEDULED]
    last = scheduled[0].queued_at if scheduled else None
    if is_scheduled_run_due(schedule, datetime.now(UTC), last):
        run_id = await admin_q.enqueue_run(conn, trigger=RunTrigger.SCHEDULED)
        log.info("queued scheduled run %s", run_id)


async def drain_queues(settings: Settings) -> dict[str, int]:
    """Work the deferred stages. Bounded per tick so no single kind starves the others.

    Detail runs *alongside* the derive -> dedup -> embed chain rather than in front of it. It is
    network-bound and spends nearly all of its budget asleep on the shared per-provider interval,
    so in series it left the cores idle for that whole stretch: with Workday's queue full, a
    measured tick was 135s of which 40s was detail doing nothing but wait. Embedding hands its
    work to a thread, so the loop is free to run the fetches while a batch encodes.

    The chain itself stays ordered -- embedding is last because it is the most expensive stage and
    depends on the other two having landed -- and every stage takes its own connection, so the two
    halves never share one.

    The halves also fail independently. A detail fetch that raised took all four stages down with
    it for hours before anyone noticed, because one exception escaping here stops the whole tick.
    Isolating them means a broken source can no longer stop embedding.
    """
    done = {"detail": 0, "derive": 0, "embed": 0, "dedup": 0}

    async with connect() as conn:
        tenants = await admin_q.enabled_tenants(conn)
    sources = {source.name: source for source in build_sources(tenants=tenants)}

    async def fetch_details() -> None:
        async with PoliteClient() as client:
            async with connect() as conn:
                done["detail"] = await workers.drain_detail(
                    conn, client, sources, limit=_DETAIL_PER_TICK
                )

    async def derive_and_embed() -> None:
        async with connect() as conn:
            done["derive"] = await workers.drain_derive(conn)
        async with connect() as conn:
            done["dedup"] = await workers.drain_dedup(conn)
        async with connect() as conn:
            done["embed"] = await workers.drain_embed(conn, limit=settings.embed_batch_size)

    halves = await asyncio.gather(
        fetch_details(), derive_and_embed(), return_exceptions=True
    )
    for name, outcome in zip(("detail", "derive/dedup/embed"), halves, strict=True):
        if isinstance(outcome, BaseException):
            log.error("queue half %r failed this tick", name, exc_info=outcome)
    return done


def _progress_control(run_id: int) -> ingest.RunControl:
    """Bridge the run queue to the sweep, in the direction that keeps ingest ignorant of runs."""

    async def starting(total: int) -> None:
        async with connect() as conn:
            await admin_q.start_run_progress(conn, run_id, total)

    async def source_done(done: int, next_source: str | None) -> None:
        async with connect() as conn:
            await admin_q.advance_run_progress(conn, run_id, done=done, current_source=next_source)

    async def cancelled() -> bool:
        async with connect() as conn:
            return await admin_q.cancel_requested(conn, run_id)

    return ingest.RunControl(starting=starting, source_done=source_done, cancelled=cancelled)


async def _execute(settings: Settings, run) -> None:
    if run.match_user_id is not None:
        await _execute_match_only(settings, run)
        return
    report = await ingest.run(
        only_source=run.only_source,
        backfill=run.backfill,
        settings=settings,
        control=_progress_control(run.id),
    )
    if report.cancelled:
        # Before matching and digests: cancelling must not still spend a user's LLM credit.
        async with connect() as conn:
            await admin_q.finish_run(
                conn,
                run.id,
                status=RunStatus.CANCELLED,
                report={"summary": report.summary(), "skipped": report.skipped},
            )
        log.info("run %s cancelled after %d source(s)", run.id, len(report.per_source))
        return

    # Not gated on the queues being empty: a user should see today's postings as soon as they
    # are usable, not once the last embedding in a 36k backlog has landed.
    matches = await matching.run_all(settings)
    notified = 0
    if settings.smtp_host:
        notified = await send_digests(settings)

    payload = {
        "summary": report.summary(),
        "sources": {name: asdict(item) for name, item in report.per_source.items()},
        "matches": [
            {
                "user_id": match.user_id,
                "retrieved": match.retrieved,
                "scored": match.scored,
                "cost_usd": str(match.cost_usd),
                "stopped_on_budget": match.stopped_on_budget,
            }
            for match in matches
        ],
        "digests_sent": notified,
    }
    async with connect() as conn:
        await admin_q.finish_run(conn, run.id, status=RunStatus.SUCCESS, report=payload)


async def _execute_match_only(settings: Settings, run) -> None:
    """Re-rank one user over the corpus as it stands; no source is touched and no digest is sent.

    The user asked for this from the page they are looking at, so the result lands there; the
    digest for anything newly scored goes out with the next scheduled run as usual.
    """
    match = await matching.run_for_user(run.match_user_id, settings)
    payload = {
        "summary": match.summary(),
        "matches": [
            {
                "user_id": match.user_id,
                "retrieved": match.retrieved,
                "scored": match.scored,
                "cost_usd": str(match.cost_usd),
                "stopped_on_budget": match.stopped_on_budget,
            }
        ],
    }
    status = RunStatus.FAILED if match.errors and not match.retrieved else RunStatus.SUCCESS
    async with connect() as conn:
        await admin_q.finish_run(
            conn, run.id, status=status, report=payload, error="; ".join(match.errors) or None
        )


async def _tick(settings: Settings) -> None:
    async with connect() as conn:
        await admin_q.fail_orphaned_runs(conn)
        await release_stale(conn)
        await _maybe_enqueue_scheduled(conn)

    drained = await drain_queues(settings)
    if any(drained.values()):
        log.info("queues drained: %s", drained)

    async with connect() as conn:
        run = await admin_q.claim_next_run(conn)
    if run is None:
        return

    log.info("starting run %s (trigger=%s)", run.id, run.trigger)
    try:
        await _execute(settings, run)
    except Exception as exc:  # noqa: BLE001 - a failed run is recorded, never a crashed runner
        log.exception("run %s failed", run.id)
        async with connect() as conn:
            await admin_q.finish_run(
                conn,
                run.id,
                status=RunStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )


async def main() -> None:
    settings = get_settings()
    log.info("runner started; tick=%ss", TICK_SECONDS)
    while True:
        try:
            await _tick(settings)
        except Exception:  # noqa: BLE001 - the loop must survive anything a tick can raise
            log.exception("runner tick failed; continuing")
        await asyncio.sleep(TICK_SECONDS)


