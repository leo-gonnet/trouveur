"""The runner: the only process that executes a scan.

Owns the schedule (a single `pipeline_schedule` row) and drains the `pipeline_run` queue. The web
UI enqueues rows and reads status but never runs the pipeline, so the two share nothing but the
database and either can be down without breaking the other. A scan crash is handled here (record,
one retry, then email).

Schedule times are UTC. Keep it that way unless someone adds a tz column and the UI to set it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import traceback
from datetime import UTC, datetime, timedelta

from trouveur.config import Settings, get_settings
from trouveur.db import queries as q
from trouveur.db.engine import connect
from trouveur.notify import email
from trouveur.pipeline import run as run_pipeline

log = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=10)
RETRY_DELAY = timedelta(minutes=30)
MAX_ATTEMPTS = 2  # one initial try plus one retry


def slot_today(schedule, now: datetime) -> datetime:
    return now.replace(
        hour=schedule.run_hour, minute=schedule.run_minute, second=0, microsecond=0
    )


def is_scheduled_run_due(schedule, now: datetime, last_queued_at: datetime | None) -> bool:
    """True when today's slot has passed and nothing has been queued for it yet."""
    if not schedule.enabled:
        return False
    slot = slot_today(schedule, now)
    if now < slot:
        return False
    return last_queued_at is None or last_queued_at < slot


async def _maybe_enqueue_scheduled(conn) -> None:
    schedule = await q.get_schedule(conn)
    latest = await q.latest_run(conn)
    last_queued_at = latest.queued_at if latest else None
    if is_scheduled_run_due(schedule, datetime.now(UTC), last_queued_at):
        run_id = await q.enqueue_run(
            conn, trigger="scheduled", lookback_days=schedule.lookback_days
        )
        log.info("queued scheduled run %d (lookback %dd)", run_id, schedule.lookback_days)


async def _execute(settings: Settings, run) -> None:
    log.info("run %d starting (trigger=%s attempt=%d)", run.id, run.trigger, run.attempts)
    try:
        report = await run_pipeline(
            since_days=run.lookback_days,
            use_llm=run.use_llm,
            send_email=run.send_email,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - the runner is the backstop; nothing above catches
        detail = traceback.format_exc()
        log.exception("run %d failed", run.id)
        async with connect() as conn:
            if run.attempts < MAX_ATTEMPTS:
                await q.requeue_run(
                    conn, run.id, not_before=datetime.now(UTC) + RETRY_DELAY, error=detail
                )
                log.info("run %d requeued, retrying in %s", run.id, RETRY_DELAY)
            else:
                await q.finish_run(conn, run.id, status="failed", report=None, error=detail)
                _alert(settings, run, exc)
        return

    async with connect() as conn:
        await q.finish_run(
            conn, run.id, status="success",
            report=dataclasses.asdict(report), error=None,
        )
    log.info("run %d complete: %s", run.id, report.summary())


def _alert(settings: Settings, run, exc: Exception) -> None:
    """Best-effort email after a run has failed for good. A missing SMTP config is not fatal."""
    subject = "Trouveur: scan run failed"
    body = (
        f"Run {run.id} ({run.trigger}) failed after {run.attempts} attempt(s).\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        "Check the Scans page in the web UI for the full traceback."
    )
    try:
        email.send(settings, subject, body, f"<pre>{body}</pre>")
    except Exception:  # noqa: BLE001 - alerting must never crash the runner
        log.warning("could not send failure alert email", exc_info=True)


async def _tick(settings: Settings) -> None:
    async with connect() as conn:
        await _maybe_enqueue_scheduled(conn)
        run = await q.claim_next_run(conn)
    if run is not None:
        await _execute(settings, run)


async def main() -> None:
    settings = get_settings()
    async with connect() as conn:
        orphaned = await q.fail_orphaned_runs(conn)
    if orphaned:
        log.warning("marked %d interrupted run(s) as failed on startup", orphaned)
    log.info("runner started; polling every %ss", POLL_INTERVAL.total_seconds())
    while True:
        try:
            await _tick(settings)
        except Exception:  # noqa: BLE001 - one bad tick must not stop the loop
            log.exception("runner tick failed")
        await asyncio.sleep(POLL_INTERVAL.total_seconds())
