"""The export, end to end, against a real corpus.

The unit tests guard the shape of the thing. This one is here because the export's value is
entirely in whether the file it wrote says what the database said -- and a projection that drops
a column, a driver that hands back a type Arrow will not take, or a cursor that stops early all
look exactly like success from the outside: a file appears, and it is wrong.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq
import pytest

# The archive is an optional extra; a checkout without it should not fail the suite.
pytest.importorskip("pyarrow")

from trouveur.archive import export
from trouveur.archive.hub import LocalDestination
from trouveur.archive.streams import DOCUMENTS, JOBS, LIFECYCLE
from trouveur.db.engine import connect
from trouveur.db.queries import archive as archive_q

pytestmark = pytest.mark.asyncio


def _past_freeze(*streams):
    """The date on which today's rows are frozen for every stream given.

    The seeded corpus is written now, so an export running today sees nothing it is allowed to
    freeze. Advancing the clock past the widest lag is what makes these tests about the
    round-trip rather than about the calendar.
    """
    lag = max(stream.lag_days for stream in streams) if streams else max(
        stream.lag_days for stream in (DOCUMENTS, JOBS, LIFECYCLE)
    )
    return (datetime.now(UTC) + timedelta(days=1 + lag)).date()


async def test_the_payload_stream_round_trips_byte_for_byte(seeded, tmp_path):
    """The archive is the only input that cannot be recomputed. It has to come back exactly."""
    destination = LocalDestination(tmp_path)
    report = await export(
        destination=destination, streams=(DOCUMENTS,), today=_past_freeze(DOCUMENTS)
    )
    assert report.written, "the seeded corpus archived no payloads at all"

    exported = {}
    for partition in report.written:
        table = pq.read_table(tmp_path / DOCUMENTS.path(partition.day))
        exported.update({row["id"]: row for row in table.to_pylist()})

    live = {}
    async with connect() as conn:
        for partition in report.written:
            after = 0
            while True:
                rows = await archive_q.documents(
                    conn, partition.day, after=after, chunk=500
                )
                if not rows:
                    break
                live.update({row.id: row for row in rows})
                after = rows[-1].id

    assert set(live) == set(exported)
    for doc_id, row in live.items():
        copy = exported[doc_id]
        assert row.payload == copy["payload"]
        assert json.loads(row.payload) == json.loads(copy["payload"])
        assert row.payload_sha256 == copy["payload_sha256"]
        assert (row.source, row.external_id, row.kind) == (
            copy["source"], copy["external_id"], copy["kind"]
        )


async def test_postings_and_their_facets_survive_the_round_trip(seeded, tmp_path):
    destination = LocalDestination(tmp_path)
    report = await export(destination=destination, streams=(JOBS,), today=_past_freeze(JOBS))
    assert report.rows > 0

    exported = {}
    for partition in report.written:
        table = pq.read_table(tmp_path / JOBS.path(partition.day))
        assert table.schema == JOBS.schema, "a partition must match the declared schema exactly"
        exported.update({row["id"]: row for row in table.to_pylist()})

    async with connect() as conn:
        rows = await archive_q.jobs(
            conn, report.written[0].day, after=0, chunk=500
        )
    assert rows, "no postings to compare against"
    for row in rows:
        copy = exported[row.id]
        for name in JOBS.schema.names:
            assert getattr(row, name) == copy[name], f"{name} differs for job {row.id}"
        # The facet join has to actually produce facets, or this test passes on all-NULL columns.
        assert copy["derive_version"] is not None


async def test_a_day_already_at_the_destination_is_never_rewritten(seeded, tmp_path):
    """Append-only is the whole safety argument: a rerun must not be able to damage the past."""
    destination = LocalDestination(tmp_path)
    first = await export(destination=destination, today=_past_freeze())
    assert first.written

    stamps = {
        path.relative_to(tmp_path).as_posix(): path.stat().st_mtime_ns
        for path in tmp_path.rglob("*.parquet")
    }
    second = await export(destination=destination, today=_past_freeze())
    assert second.written == []
    assert second.skipped == len(stamps)
    after = {
        path.relative_to(tmp_path).as_posix(): path.stat().st_mtime_ns
        for path in tmp_path.rglob("*.parquet")
    }
    assert after == stamps, "a rerun rewrote a partition it should have skipped"


async def test_an_interrupted_run_is_finished_by_the_next_one(seeded, tmp_path):
    destination = LocalDestination(tmp_path)
    await export(destination=destination, today=_past_freeze())
    lost = sorted(tmp_path.rglob("*.parquet"))[0]
    name = lost.relative_to(tmp_path).as_posix()
    lost.unlink()

    report = await export(destination=destination, today=_past_freeze())
    assert [part for part in report.written], "the missing partition was not rebuilt"
    assert (tmp_path / name).exists()


async def test_today_is_never_frozen(seeded, tmp_path):
    """Today is still being written to. Freezing it would archive a half-finished day forever."""
    destination = LocalDestination(tmp_path)
    today = datetime.now(UTC).date()
    report = await export(destination=destination, streams=(DOCUMENTS,), today=today)
    assert all(part.day < today for part in report.written)
    assert not (tmp_path / DOCUMENTS.path(today)).exists()


async def test_a_stream_with_a_lag_leaves_recent_days_alone(seeded, tmp_path):
    """A posting's description arrives on a later fetch than its listing."""
    destination = LocalDestination(tmp_path)
    today = datetime.now(UTC).date()
    report = await export(destination=destination, streams=(JOBS,), today=today)
    cutoff = today - timedelta(days=1 + JOBS.lag_days)
    assert all(part.day <= cutoff for part in report.written)


async def test_an_empty_day_is_not_uploaded_as_an_empty_file(seeded, tmp_path):
    """A zero-row file asserts 'this day is done' about a day that may yet be backfilled."""
    destination = LocalDestination(tmp_path)
    report = await export(
        destination=destination, streams=(LIFECYCLE,), today=_past_freeze(LIFECYCLE)
    )
    for path in tmp_path.rglob("*.parquet"):
        assert pq.read_table(path).num_rows > 0
    assert report.rows == sum(part.rows for part in report.written)
