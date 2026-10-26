"""Sweep orchestration. No parsing, no SQL, no interpretation.

If a change to a source's behaviour requires editing this file, the abstraction has leaked.
Failure is isolated per source: one dead source is a recorded warning, never a lost run.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from trouveur.config import Settings, get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import freshness
from trouveur.db.queries import ingest as q
from trouveur.db.queries import jobs as jobs_q
from trouveur.ingest.persist import persist
from trouveur.models import RawDocument
from trouveur.sources import build_sources
from trouveur.sources.base import Source, SweepOutcome
from trouveur.sources.errors import SourceBlocked
from trouveur.sources.http import PoliteClient
from trouveur.sources.registry import SOURCES

log = logging.getLogger(__name__)

# A day, not the detail queue's hour: runs are nightly, so a shorter pause never skips one.
SWEEP_PAUSE = timedelta(days=1)
# A source stays slow this long after its last block, so one good sweep does not bring back the
# pace that got it blocked.
SLOW_DAYS = 14
MAX_SLOWDOWN = 16


def pause_after(blocks: int) -> timedelta:
    """How long a source is left alone after `blocks` blocked sweeps in a row: doubling, a week
    at most."""
    return min(SWEEP_PAUSE * 2 ** min(max(blocks - 1, 0), 8), timedelta(days=7))


def slowdown(recent_blocks: int) -> int:
    return min(2 ** min(recent_blocks, 8), MAX_SLOWDOWN)


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
    blocked: str | None = None
    answered: set[str] = field(default_factory=set)

    @property
    def coverage_shortfall(self) -> int:
        """Postings the source said existed that the sweep never saw. Reported rather than
        assumed to be zero: a partitioned sweep has a blind spot by construction."""
        if self.expected is None:
            return 0
        return max(0, self.expected - self.documents)


@dataclass
class RunControl:
    """How a caller watches a sweep and asks it to stop. Callbacks rather than a run id, so this
    module keeps knowing nothing about the run queue."""

    starting: Callable[[int], Awaitable[None]] | None = None
    source_done: Callable[[int, str | None], Awaitable[None]] | None = None
    cancelled: Callable[[], Awaitable[bool]] | None = None

    async def should_stop(self) -> bool:
        return bool(self.cancelled and await self.cancelled())


@dataclass
class IngestReport:
    per_source: dict[str, SourceReport] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    # Sources left out of this run because their last sweeps were blocked, and until when.
    paused: dict[str, datetime] = field(default_factory=dict)
    pruned: int = 0
    # Discovered boards this run swept once. Their verdict comes later, once the derive queue has
    # given their postings facets: see trouveur.discovery.trial.
    trialled: int = 0

    @property
    def cancelled(self) -> bool:
        return bool(self.skipped)

    def summary(self) -> str:
        parts = []
        for name, report in self.per_source.items():
            state = (
                "blocked" if report.blocked
                else "error" if report.error
                else "complete" if report.complete
                else "delta"
            )
            parts.append(
                f"{name}[{state}] docs={report.documents} new/changed={report.changed} "
                f"closed={report.closed}"
            )
        if self.paused:
            parts.append(f"paused after a block: {', '.join(sorted(self.paused))}")
        if self.pruned:
            parts.append(f"pruned {self.pruned} aged-out vector(s)")
        if self.trialled:
            parts.append(f"tried {self.trialled} discovered board(s)")
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
    # The boards on trial are merged into the crawl set and the sweep is told nothing: they are
    # fetched, archived, closed and recorded exactly like a board somebody promoted by hand, and
    # the only difference is the verdict the runner reaches afterwards (`discovery/trial.py`). A
    # separate pass would be a second sweep path to keep correct.
    disabled = [name.strip() for name in settings.disabled_sources.split(",") if name.strip()]
    # What the trial queue is restricted to: a disabled source's candidates are never swept, so
    # never stamped, so they would take the cap's slots again on every run.
    async with connect() as conn:
        history = await q.block_history(conn, days=SLOW_DAYS)
    now = datetime.now(UTC)
    # `--source` is an operator asking for this one source now, so it is not held back.
    paused = {
        source: row.last_blocked_at + pause_after(row.streak)
        for source, row in history.items()
        if source in SOURCES
        and only_source is None
        and row.streak
        and now < row.last_blocked_at + pause_after(row.streak)
    }
    disabled += list(paused)
    askable = sorted(set(SOURCES) - set(disabled))
    async with connect() as conn:
        tenants = await admin_q.enabled_tenants(conn)
        trials = await admin_q.tenants_due_for_trial(
            conn,
            limit=settings.trial_boards_per_run,
            recheck_days=settings.trial_recheck_days,
            sources=askable,
            source=only_source,
        )
    # The enabled boards first: a block met on a trial board stops the source, and must not cost
    # the boards we already sweep their night.
    for source, scopes in trials.items():
        enabled = tenants.get(source, [])
        tenants[source] = enabled + sorted(set(scopes) - set(enabled))

    sources = build_sources(tenants=tenants, only=only_source, disabled=disabled)
    # Filtered here as well as in the query, which `--source` and a missing adapter do not reach:
    # marking a board tried that nothing asked retires it for a month on a sweep that never ran.
    swept = {source.name for source in sources}
    trials = {source: scopes for source, scopes in trials.items() if source in swept}

    delays = {
        source.name: (SOURCES[source.name].min_interval or settings.request_delay_seconds)
        * slowdown(history[source.name].recent if source.name in history else 0)
        for source in sources
        if source.name in history or SOURCES[source.name].min_interval is not None
    }
    async with PoliteClient() as client:
        report = await sweep_sources(
            client, sources, settings=settings, backfill=backfill, control=control,
            delays=delays,
        )
    report.paused = paused

    # Before the verdict and regardless of what it will be: a board that 404s, or a run that dies
    # in the next stage, must not be back in tomorrow's trial queue -- it would be swept every
    # night for ever, which is the one failure the cap exists to prevent. Only the sources a
    # cancelled run never reached are left unstamped; the boards of a source it did sweep were
    # asked, and asking them again tomorrow would spend the cap twice over. A block stops the sweep
    # partway, so there only the boards that answered were asked.
    tried = {}
    for source, scopes in trials.items():
        if source in report.skipped:
            continue
        swept = report.per_source.get(source)
        if swept and swept.blocked:
            scopes = [scope for scope in scopes if scope in swept.answered]
        tried[source] = scopes
    if tried:
        async with connect() as conn:
            report.trialled = await admin_q.record_trials(conn, tried)

    # Unconditional, including after a cancelled run: unlike retirement, age is known without
    # asking any source.
    report.pruned = await _prune_aged_out(settings)
    return report


async def sweep_sources(
    client: PoliteClient,
    sources: Sequence[Source],
    *,
    settings: Settings,
    backfill: bool = False,
    control: RunControl | None = None,
    delays: dict[str, float] | None = None,
) -> IngestReport:
    """Sweep an already-assembled list of sources, one client shared across all of them.

    `delays` paces a source more slowly than the client's default, per provider as always.
    """
    report = IngestReport()
    if control and control.starting:
        await control.starting(len(sources))

    for index, source in enumerate(sources):
        # Between sources, NEVER inside one: a source stopped mid-flight has seen only part of
        # its live set, and closing reads exactly that.
        if control and await control.should_stop():
            report.skipped = [later.name for later in sources[index:]]
            log.info("run cancelled before %s", report.skipped[0])
            break
        delay = (delays or {}).get(source.name)
        paced = client if delay is None else client.paced(delay)
        report.per_source[source.name] = await _sweep_source(paced, source, settings, backfill)
        if control and control.source_done:
            remaining = sources[index + 1 :]
            await control.source_done(index + 1, remaining[0].name if remaining else None)

    log.info("ingest complete: %s", report.summary())
    return report


async def _prune_aged_out(settings: Settings) -> int:
    """Drop vectors for postings past the horizon, in chunks, until none are left."""
    cutoff = freshness.fresh_since(settings.retrieval_horizon_days)
    total = 0
    while True:
        async with connect() as conn:
            removed = await jobs_q.prune_stale_embeddings(conn, cutoff)
        total += removed
        if removed == 0:
            return total


async def _sweep_source(
    client: PoliteClient, source: Source, settings: Settings, backfill: bool
) -> SourceReport:
    report = SourceReport()
    async with connect() as conn:
        sweep_id, started_at = await q.start_sweep(conn, source.name)

    async def sink(documents: Sequence[RawDocument]) -> None:
        # One transaction per batch, not one per sweep.
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
        # Per batch, not at the end: without it a long source and a wedged one look identical.
        async with connect() as conn:
            await q.record_sweep_progress(conn, sweep_id, documents_seen=report.documents)

    outcome = SweepOutcome()
    error: str | None = None
    try:
        outcome = await source.sweep(client, sink, backfill=backfill)
    except Exception as exc:  # noqa: BLE001 - per-source isolation; one dead source is not a run
        error = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, SourceBlocked):
            outcome.blocked = str(exc)
        log.warning("source %s failed: %s", source.name, exc, exc_info=True)
    report.blocked = outcome.blocked
    report.answered = {result.scope for result in outcome.scope_results if not result.blocked}

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
            error=report.error or report.blocked,
            blocked=report.blocked is not None,
        )
    return report


async def _apply_lifecycle(
    source: Source, outcome: SweepOutcome, started_at, settings: Settings
) -> int:
    """Retire postings, by evidence where there is any and by age where there is not."""
    async with connect() as conn:
        if outcome.closable_scopes:
            return await q.close_unseen(conn, source.name, outcome.closable_scopes, started_at)
        # No scope seen in full, so absence proves nothing and age is the only signal left.
        return await q.close_stale(
            conn, source.name, timedelta(days=settings.stale_close_days)
        )
