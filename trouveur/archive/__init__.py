"""Offsite export of the corpus, one immutable day-partition at a time.

The archive is the only thing in this system that cannot be recomputed -- everything downstream
of it is a pure function of it -- and it lives on one VPS with one disk. This puts a copy
somewhere else, nightly, without ever rewriting what it already wrote.

The design is one idea: **a day is a file, and a file is written once.** Everything follows from
it. There is no cursor to keep in sync, because "which days are done" is read back from the
destination. A run that dies halfway is resumed by the next one, because the days it finished are
already there. A day that was empty is simply re-checked, cheaply, until it has something in it.
And a partition can never disagree with the database about the past, because nothing ever goes
back and edits one.

What a day is safe to freeze on differs per stream and is `Stream.lag_days`; see `streams`.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trouveur.archive import hub
from trouveur.archive.streams import ALL, Stream
from trouveur.config import Settings, get_settings
from trouveur.db.engine import connect

log = logging.getLogger(__name__)

PAGE = 2000


@dataclass
class Partition:
    stream: str
    day: date
    rows: int
    bytes: int

    def __str__(self) -> str:
        rows = f"{self.stream}/{self.day} {self.rows} rows"
        # Size is only known once the file exists, which on a dry run it does not.
        return rows if self.bytes == 0 else f"{rows}, {self.bytes / 1e6:.1f} MB"


@dataclass
class ExportReport:
    destination: str
    written: list[Partition] = field(default_factory=list)
    skipped: int = 0
    dry_run: bool = False

    @property
    def rows(self) -> int:
        return sum(part.rows for part in self.written)

    @property
    def bytes(self) -> int:
        return sum(part.bytes for part in self.written)

    def summary(self) -> str:
        if self.dry_run:
            return (
                f"would write {len(self.written)} partition(s), {self.rows} rows "
                f"to {self.destination} ({self.skipped} already present)"
            )
        return (
            f"wrote {len(self.written)} partition(s), {self.rows} rows, "
            f"{self.bytes / 1e6:.1f} MB to {self.destination} "
            f"({self.skipped} already present)"
        )


async def _write_partition(stream: Stream, day: date, path: Path) -> int:
    """Stream one day into a Parquet file by keyset. Returns the row count."""
    written = 0
    writer = None
    try:
        async with connect() as conn:
            after = 0
            while True:
                rows = await stream.page(conn, day, after=after, chunk=PAGE)
                if not rows:
                    break
                batch = pa.RecordBatch.from_pydict(
                    {
                        name: [getattr(row, name) for row in rows]
                        for name in stream.schema.names
                    },
                    schema=stream.schema,
                )
                if writer is None:
                    writer = pq.ParquetWriter(path, stream.schema, compression="zstd")
                writer.write_batch(batch)
                written += len(rows)
                after = getattr(rows[-1], stream.key)
    finally:
        if writer is not None:
            writer.close()
    return written


async def export(
    settings: Settings | None = None,
    *,
    destination: hub.Destination | None = None,
    streams: tuple[Stream, ...] = ALL,
    today: date | None = None,
    dry_run: bool = False,
) -> ExportReport:
    settings = settings or get_settings()
    destination = destination or hub.destination(
        settings.archive_repo, settings.archive_token
    )
    today = today or datetime.now(UTC).date()

    present = destination.existing()
    report = ExportReport(destination=destination.describe(), dry_run=dry_run)

    for stream in streams:
        async with connect() as conn:
            counts = await stream.counts(conn)
        # Never freeze a day that can still change: today is still being written to, and a
        # stream with a lag is still filling in days behind it.
        frozen = today - timedelta(days=1 + stream.lag_days)
        for day in sorted(counts):
            if day > frozen:
                continue
            path_in_repo = stream.path(day)
            if path_in_repo in present:
                report.skipped += 1
                continue
            if dry_run:
                report.written.append(Partition(stream.name, day, counts[day], 0))
                continue
            with tempfile.TemporaryDirectory() as scratch:
                local = Path(scratch) / f"{stream.name}-{day}.parquet"
                rows = await _write_partition(stream, day, local)
                if rows == 0:
                    # The count said there was something here and the page found nothing, so a
                    # row went away between the two. Uploading an empty file would assert "this
                    # day is done" about a day that is not.
                    log.warning("%s for %s emptied while being read", stream.name, day)
                    continue
                size = local.stat().st_size
                destination.put(
                    local, path_in_repo, f"archive {stream.name} for {day.isoformat()}"
                )
            report.written.append(Partition(stream.name, day, rows, size))
            log.info("archived %s", report.written[-1])

    return report
