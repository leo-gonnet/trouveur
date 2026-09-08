"""Sweep orchestration. No parsing, no SQL, no interpretation.

This module decides what runs, in what order, and what happens when a source fails. Everything it
touches lives behind a function in sources/, db/queries/ or work/ -- if a change to a source's
behaviour requires editing this file, the source abstraction has leaked.

Failure is isolated per source. One dead source is a recorded warning, never a lost run: the
sources that did work still land their postings, and the dashboard shows the one that did not.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
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
class IngestReport:
    per_source: dict[str, SourceReport] = field(default_factory=dict)

    def summary(self) -> str:
        parts = []
        for name, report in self.per_source.items():
            state = "error" if report.error else ("complete" if report.complete else "delta")
            parts.append(
                f"{name}[{state}] docs={report.documents} new/changed={report.changed} "
                f"closed={report.closed}"
            )
        return " | ".join(parts) or "nothing ran"


async def run(
    *, only_source: str | None = None, backfill: bool = False, settings: Settings | None = None
) -> IngestReport:
    settings = settings or get_settings()
    report = IngestReport()

    async with connect() as conn:
        boards = await admin_q.enabled_greenhouse_boards(conn)
    sources = build_sources(greenhouse_boards=boards, only=only_source)

    async with PoliteClient() as client:
        for source in sources:
            report.per_source[source.name] = await _sweep_source(client, source, settings, backfill)
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
