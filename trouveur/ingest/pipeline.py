"""Sweep orchestration. No parsing, no SQL, no interpretation.

This module decides what runs, in what order, and what happens when a source fails. Everything it
touches lives behind a function in sources/, db/queries/ or work/ -- if a change to a source's
behaviour requires editing this file, the source abstraction has leaked.

Failure is isolated per source. One dead source is a recorded warning, never a lost run: the
sources that did work still land their postings, and the dashboard shows the one that did not.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from trouveur.config import Settings, get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import ingest as q
from trouveur.ingest.persist import persist
from trouveur.models import RawDocument
from trouveur.sources import build_sources
from trouveur.sources.base import Source, SweepOutcome
from trouveur.sources.http import PoliteClient

log = logging.getLogger(__name__)


@dataclass
class SourceReport:
    documents: int = 0
    upserted: int = 0
    changed: int = 0
    closed: int = 0
    expected: int | None = None
    partitions_total: int = 0
    partitions_done: int = 0
    partitions_overflowed: int = 0
    complete: bool = False
    error: str | None = None

    @property
    def coverage_shortfall(self) -> int:
        """Postings the source said existed that the sweep never saw.

        Reported rather than assumed to be zero: the Arbeitsagentur berufsfeld facet does not
        quite sum to its own total, so a partitioned sweep has a small blind spot by construction.
        A number that starts growing is the signal that partitioning has stopped covering.
        """
        if self.expected is None:
            return 0
        return max(0, self.expected - self.documents)


@dataclass
class RunControl:
    """How a caller watches a sweep and asks it to stop.

    Callbacks rather than a run id, so this module keeps knowing nothing about the run queue: the
    runner owns what a "run" is, and ingest owns what a sweep is. It also makes cancellation
    testable without a database.
    """

    starting: Callable[[int], Awaitable[None]] | None = None
    source_done: Callable[[int, str | None], Awaitable[None]] | None = None
    cancelled: Callable[[], Awaitable[bool]] | None = None

    async def should_stop(self) -> bool:
        return bool(self.cancelled and await self.cancelled())


@dataclass
class IngestReport:
    per_source: dict[str, SourceReport] = field(default_factory=dict)
    # Sources never reached because the run was cancelled. Named rather than counted so the
    # difference between "found nothing" and "never asked" survives into the report.
    skipped: list[str] = field(default_factory=list)

    @property
    def cancelled(self) -> bool:
        return bool(self.skipped)

    def summary(self) -> str:
        parts = []
        for name, report in self.per_source.items():
            state = "error" if report.error else ("complete" if report.complete else "delta")
            parts.append(
                f"{name}[{state}] docs={report.documents} new/changed={report.changed} "
                f"closed={report.closed}"
            )
        if self.skipped:
            parts.append(f"cancelled before {', '.join(self.skipped)}")
        return " | ".join(parts) or "nothing ran"


async def run(
    *,
    only_source: str | None = None,
    backfill: bool = False,
    settings: Settings | None = None,
    control: RunControl | None = None,
) -> IngestReport:
    settings = settings or get_settings()
    async with connect() as conn:
        tenants = await admin_q.enabled_tenants(conn)
    sources = build_sources(tenants=tenants, only=only_source)

    async with PoliteClient() as client:
        return await sweep_sources(
            client, sources, settings=settings, backfill=backfill, control=control
        )


async def sweep_sources(
    client: PoliteClient,
    sources: Sequence[Source],
    *,
    settings: Settings,
    backfill: bool = False,
    control: RunControl | None = None,
) -> IngestReport:
    """Sweep an already-assembled list of sources, one client shared across all of them.

    Takes the sources rather than building them so the evaluation harness can substitute a
    narrowed Arbeitsagentur without carrying a second copy of this loop -- and so the politeness
    budget, which lives on the client, is shared by everything the caller sweeps.
    """
    report = IngestReport()
    if control and control.starting:
        await control.starting(len(sources))

    for index, source in enumerate(sources):
        # Checked between sources, never inside one. A source stopped mid-flight has seen only
        # part of its live set, and the machinery that decides what to retire reads exactly that
        # -- so an interrupted sweep must not be able to reach the closing step at all.
        if control and await control.should_stop():
            report.skipped = [later.name for later in sources[index:]]
            log.info("run cancelled before %s", report.skipped[0])
            break
        report.per_source[source.name] = await _sweep_source(client, source, settings, backfill)
        if control and control.source_done:
            remaining = sources[index + 1 :]
            await control.source_done(index + 1, remaining[0].name if remaining else None)

    log.info("ingest complete: %s", report.summary())
    return report


async def _sweep_source(
    client: PoliteClient, source: Source, settings: Settings, backfill: bool
) -> SourceReport:
    report = SourceReport()
    async with connect() as conn:
        sweep_id, started_at = await q.start_sweep(conn, source.name)

    async def sink(documents: Sequence[RawDocument]) -> None:
        # One transaction per batch, not one per sweep. A sweep of tens of thousands of documents
        # inside a single transaction would hold locks for its whole duration and lose everything
        # if it failed near the end.
        async with connect() as conn:
            await q.archive_documents(conn, documents)
            result = await persist(
                conn,
                source.name,
                [doc.external_id for doc in documents],
                requires_detail=source.requires_detail,
            )
        report.documents += len(documents)
        report.upserted += result.upserted
        report.changed += len(result.changed)
        # Published per batch, not at the end. The row already exists -- start_sweep inserted it
        # before the first request -- so this is what makes a long source visibly moving rather
        # than indistinguishable from a wedged one. One UPDATE per batch, and batches are
        # hundreds of documents, so it does not show up next to the fetch it follows.
        async with connect() as conn:
            await q.record_sweep_progress(conn, sweep_id, documents_seen=report.documents)

    outcome = SweepOutcome()
    error: str | None = None
    try:
        outcome = await source.sweep(client, sink, backfill=backfill)
    except Exception as exc:  # noqa: BLE001 - per-source isolation; one dead source is not a run
        error = f"{type(exc).__name__}: {exc}"
        log.warning("source %s failed: %s", source.name, exc, exc_info=True)

    report.error = error or (
        "; ".join(outcome.errors)[:2000] if outcome.errors else None
    )
    report.expected = outcome.expected
    report.partitions_total = outcome.partitions_total
    report.partitions_done = outcome.partitions_done
    report.partitions_overflowed = outcome.partitions_overflowed
    report.complete = outcome.complete

    if outcome.scope_results:
        async with connect() as conn:
            await admin_q.record_scope_health(conn, source.name, outcome.scope_results)
            # Removing a slug from the registry file should also clear its health row, or the
            # dashboard keeps reporting a tenant nobody crawls any more.
            await admin_q.prune_scope_health(
                conn, source.name, [result.scope for result in outcome.scope_results]
            )

    if error is None:
        report.closed = await _apply_lifecycle(source, outcome, started_at, settings)
    if report.coverage_shortfall:
        log.warning(
            "source %s saw %d of %d postings it was told exist (%d unaccounted for)",
            source.name, report.documents, outcome.expected, report.coverage_shortfall,
        )

    async with connect() as conn:
        await q.finish_sweep(
            conn, sweep_id,
            ok=error is None and not outcome.errors,
            complete=outcome.complete,
            partitions_total=outcome.partitions_total,
            partitions_done=outcome.partitions_done,
            partitions_overflowed=outcome.partitions_overflowed,
            documents_seen=report.documents,
            jobs_upserted=report.upserted,
            jobs_closed=report.closed,
            error=report.error,
        )
    return report


async def _apply_lifecycle(
    source: Source, outcome: SweepOutcome, started_at, settings: Settings
) -> int:
    """Retire postings, by evidence where there is any and by age where there is not."""
    async with connect() as conn:
        if outcome.closable_scopes:
            # The sweep saw these scopes in full, so anything it did not see is genuinely gone.
            return await q.close_unseen(conn, source.name, outcome.closable_scopes, started_at)
        # No scope was observed in full -- a delta sweep, for instance -- so absence proves
        # nothing and the only available signal is age. Explicitly a heuristic; see close_stale.
        return await q.close_stale(
            conn, source.name, timedelta(days=settings.stale_close_days)
        )
