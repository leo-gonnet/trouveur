"""Pipeline orchestration tests that don't need a database.

`collect()` and `_record_source_health()` are tested directly with fake sources, since a full
`run()` needs Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trouveur.models import Job, ProfileData
from trouveur.pipeline import RunReport, build_sources, collect


class _WorkingSource:
    name = "good"

    async def fetch(self, client, since):
        for i in range(3):
            yield Job(source=self.name, source_native_id=str(i), url=f"u{i}", title=f"job {i}")


class _BrokenSource:
    name = "broken"

    async def fetch(self, client, since):
        if False:
            yield  # pragma: no cover - makes this an async generator
        raise RuntimeError("upstream returned garbage")


class _PartiallyBrokenSource:
    """Yields some jobs, then blows up mid-stream."""

    name = "flaky"

    async def fetch(self, client, since):
        yield Job(source=self.name, source_native_id="1", url="u", title="one job before failure")
        raise ConnectionError("connection reset")


@pytest.fixture
def since() -> datetime:
    return datetime.now(UTC)


async def test_collect_gathers_jobs_from_a_working_source(since):
    report = RunReport()
    collected = await collect([_WorkingSource()], since, report)
    assert len(collected) == 3
    assert report.collected == 3
    assert report.per_source == {"good": 3}
    assert report.errors == {}


async def test_a_broken_source_does_not_stop_the_others(since):
    """Per-source isolation: one dead source must never abort the run (see AGENTS.md)."""
    report = RunReport()
    collected = await collect([_BrokenSource(), _WorkingSource()], since, report)
    assert len(collected) == 3
    assert "broken" in report.errors
    assert report.per_source["broken"] == 0
    assert report.per_source["good"] == 3


async def test_jobs_yielded_before_a_mid_stream_failure_are_kept(since):
    report = RunReport()
    collected = await collect([_PartiallyBrokenSource()], since, report)
    assert len(collected) == 1
    assert report.per_source["flaky"] == 1
    assert "flaky" in report.errors


async def test_every_configured_source_gets_a_per_source_count_even_when_empty(since):
    class _EmptySource:
        name = "empty"

        async def fetch(self, client, since):
            return
            yield  # pragma: no cover

    report = RunReport()
    await collect([_EmptySource()], since, report)
    assert report.per_source == {"empty": 0}


def test_build_sources_omits_personio_when_no_tenants_are_configured():
    sources = build_sources(ProfileData(), tenants=[])
    assert "personio" not in {source.name for source in sources}


def test_build_sources_includes_personio_when_tenants_exist():
    sources = build_sources(ProfileData(), tenants=["acme-gmbh"])
    personio = next(s for s in sources if s.name == "personio")
    assert personio.tenants == ["acme-gmbh"]
