"""The runner: the only process that sweeps sources and drains queues.

It owns the schedule and the run queue. The web app enqueues rows and reads status; it never
executes a scan. The two share nothing but Postgres, so either can be restarted, upgraded or fall
over without taking the other with it.

Each tick does queue work first and run work second. That ordering matters: detail fetches,
derivation and embedding are what make yesterday's sweep usable, and a runner that only worked
during a scan would leave the corpus permanently one step behind itself.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import UTC, datetime

from trouveur.config import Settings, get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.ingest import pipeline as ingest
from trouveur.ingest import workers
from trouveur.match import pipeline as matching
from trouveur.notify import send_digests
from trouveur.sources import build_sources
from trouveur.sources.http import PoliteClient
from trouveur.work import release_stale

log = logging.getLogger(__name__)

TICK_SECONDS = 20
_DETAIL_PER_TICK = 40


def slot_today(schedule, now: datetime) -> datetime:
    return now.replace(
        hour=schedule.run_hour, minute=schedule.run_minute, second=0, microsecond=0
    )


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
    scheduled = [run for run in runs if run.trigger == "scheduled"]
    last = scheduled[0].queued_at if scheduled else None
    if is_scheduled_run_due(schedule, datetime.now(UTC), last):
        run_id = await admin_q.enqueue_run(conn, trigger="scheduled")
        log.info("queued scheduled run %s", run_id)


async def drain_queues(settings: Settings) -> dict[str, int]:
    """Work the deferred stages. Bounded per tick so no single kind starves the others."""
    done = {"detail": 0, "derive": 0, "embed": 0, "dedup": 0}

    async with connect() as conn:
        boards = await admin_q.enabled_greenhouse_boards(conn)
    sources = {source.name: source for source in build_sources(greenhouse_boards=boards)}

    async with PoliteClient() as client:
        async with connect() as conn:
            done["detail"] = await workers.drain_detail(
                conn, client, sources, limit=_DETAIL_PER_TICK
            )

    async with connect() as conn:
        done["derive"] = await workers.drain_derive(conn)
    async with connect() as conn:
        done["dedup"] = await workers.drain_dedup(conn)
    # Embedding last: it is the most expensive stage and depends on the other two having landed.
    async with connect() as conn:
        done["embed"] = await workers.drain_embed(conn, limit=settings.embed_batch_size)
    return done


async def _execute(settings: Settings, run) -> None:
    report = await ingest.run(
        only_source=run.only_source, backfill=run.backfill, settings=settings
    )

    # Matching runs after ingest, but deliberately not gated on the queues being empty: a user
    # should see today's postings ranked as soon as they are usable, not only once the last
    # embedding in a 36k backlog has landed.
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
        await admin_q.finish_run(conn, run.id, status="success", report=payload)


async def _tick(settings: Settings) -> None:
    async with connect() as conn:
        await admin_q.fail_orphaned_runs(conn)
        # A claim outliving its worker would otherwise pin those items forever.
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
                conn, run.id, status="failed", error=f"{type(exc).__name__}: {exc}"
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


