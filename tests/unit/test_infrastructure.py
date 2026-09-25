"""Guards for the embedding seam, credential handling, the work queue and the schedule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trouveur.ingest.embed import EMBEDDING_DIM, DeterministicProvider, version_of
from trouveur.ingest.embed.text import embedding_text
from trouveur.runner.service import is_scheduled_run_due, slot_today
from trouveur.work import WorkKind
from trouveur.work.queue import _stale_query


class _Schedule:
    def __init__(self, enabled=True, run_hour=7, run_minute=0):
        self.enabled = enabled
        self.run_hour = run_hour
        self.run_minute = run_minute


async def test_embedding_provider_is_deterministic_and_correctly_sized():
    provider = DeterministicProvider()
    first = await provider.embed_documents(["Wirtschaftsingenieur München"])
    again = await provider.embed_documents(["Wirtschaftsingenieur München"])
    assert first == again
    assert len(first[0]) == EMBEDDING_DIM


async def test_documents_and_queries_are_embedded_asymmetrically():
    """e5 models are trained with distinct passage/query prefixes.

    Embedding both sides identically costs real recall while looking like it works, so the
    provider owns the prefixes rather than trusting every caller to remember them.
    """
    provider = DeterministicProvider()
    as_document = await provider.embed_documents(["process engineer"])
    as_query = await provider.embed_queries(["process engineer"])
    assert as_document != as_query


def test_embedding_version_names_the_vector_space():
    """An integer could not express that two rows came from different spaces.

    Mixing spaces in one column returns confident nonsense rather than an error, so the version
    string carries provider, model and width.
    """
    version = version_of(DeterministicProvider())
    assert version == "deterministic:sha256:384"
    assert str(EMBEDDING_DIM) in version


def test_a_query_is_embedded_by_the_model_that_wrote_the_space_it_reads(monkeypatch):
    """A width cannot say which model produced a space, and a query is compared against one.

    Mid-backfill the worker writes 768 while the dense arm still reads 384. Embedding the query
    with the writing model there is not a subtle loss of recall: pgvector refuses it outright --
    "different halfvec dimensions 768 and 384" -- so every recommendation fails for as long as the
    backfill runs, which is the exact window the two width settings exist to make safe.
    """
    from trouveur.ingest.embed import get_provider, get_query_provider

    monkeypatch.setenv("EMBEDDING_PROVIDER", "local-onnx-mpnet")
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    monkeypatch.setenv("EMBEDDING_READ_DIM", "384")
    monkeypatch.setenv("EMBEDDING_READ_PROVIDER", "local-onnx")

    assert get_provider().dim == 768, "the worker must write the new space"
    assert get_query_provider().dim == 384, "retrieval must ask the old model for its query"


def test_reads_follow_writes_when_no_read_provider_is_named(monkeypatch):
    """Every day that is not a migration, the two are one model and need no second setting."""
    from trouveur.ingest.embed import get_provider, get_query_provider

    monkeypatch.setenv("EMBEDDING_PROVIDER", "deterministic")
    monkeypatch.delenv("EMBEDDING_READ_PROVIDER", raising=False)

    assert get_query_provider().name == get_provider().name


def test_a_read_provider_that_does_not_fit_the_read_width_is_refused(monkeypatch):
    """Rejected loudly, because the alternative is confident nonsense from the wrong space."""
    from trouveur.ingest.embed import get_query_provider

    monkeypatch.setenv("EMBEDDING_PROVIDER", "local-onnx")
    monkeypatch.setenv("EMBEDDING_READ_DIM", "384")
    monkeypatch.setenv("EMBEDDING_READ_PROVIDER", "local-onnx-mpnet")

    with pytest.raises(RuntimeError, match="768"):
        get_query_provider()


def test_retrieval_asks_for_the_read_side_provider_not_the_writing_one():
    """The call site is the whole fix; a helper nothing calls would be worth nothing."""
    import inspect

    from trouveur.match import retrieve

    source = inspect.getsource(retrieve)
    assert "get_query_provider()" in source
    assert "get_provider()" not in source


def test_embedding_text_puts_discriminating_fields_before_prose():
    text = embedding_text("Process Engineer", "ACME", ["Wien"], "x" * 5000)
    assert text.startswith("Process Engineer\nACME\nWien")
    # Bounded on purpose: the model's context is finite, so the cut is chosen, not accidental.
    assert len(text) < 2000


def test_credentials_round_trip_without_leaking_the_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "a-test-encryption-secret")
    from trouveur import crypto

    crypto._cipher.cache_clear()
    from trouveur.config import get_settings

    assert get_settings().encryption_key == "a-test-encryption-secret"

    secret = "sk-or-v1-supersecrettoken"
    token = crypto.encrypt(secret)
    assert crypto.decrypt(token) == secret
    assert secret.encode() not in token
    # The fingerprint must not be key material: it is shown in a UI and pasted into support threads.
    assert secret[-4:] not in crypto.fingerprint(secret)
    crypto._cipher.cache_clear()


def test_encryption_refuses_the_development_default(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "dev-only-insecure-encryption-key")
    from trouveur import crypto

    crypto._cipher.cache_clear()
    with pytest.raises(crypto.CredentialError, match="ENCRYPTION_KEY"):
        crypto.encrypt("anything")
    crypto._cipher.cache_clear()


_CUTOFF = datetime(2026, 9, 14, tzinfo=UTC)


def test_detail_work_is_never_refilled():
    """Only the source knows whether it has a detail phase.

    Refilling detail centrally would mean encoding that per-source fact in the queue, which is
    exactly the leak the registry exists to prevent.
    """
    with pytest.raises(ValueError, match="never refilled"):
        _stale_query(WorkKind.DETAIL, "1", 0, 10, _CUTOFF)


def test_refill_queries_select_only_out_of_date_rows():
    for kind in (WorkKind.DERIVE, WorkKind.DEDUP, WorkKind.EMBED):
        sql = str(_stale_query(kind, "2", 0, 10, _CUTOFF).compile())
        assert "LIMIT" in sql
        # Keyset, not OFFSET: this runs over the whole corpus after a version bump, and OFFSET
        # would re-scan everything it had already skipped on every successive chunk.
        assert "OFFSET" not in sql


def test_embed_refill_ignores_closed_postings():
    sql = str(_stale_query(WorkKind.EMBED, "v", 0, 10, _CUTOFF).compile())
    assert "closed_at IS NULL" in sql


def test_embed_refill_ignores_postings_past_the_horizon():
    """Without this the pruner and the refill chase each other: embed, prune, embed, prune."""
    sql = str(_stale_query(WorkKind.EMBED, "v", 0, 10, _CUTOFF).compile())
    assert "coalesce(job.posted_at, job.first_seen_at)" in sql.lower()


def test_only_embedding_is_bounded_by_age():
    """Derivation and dedup are facts about a posting, true however old it is."""
    for kind in (WorkKind.DERIVE, WorkKind.DEDUP):
        sql = str(_stale_query(kind, "2", 0, 10, _CUTOFF).compile()).lower()
        assert "first_seen_at" not in sql


@pytest.fixture
def vienna(monkeypatch):
    """Pin the zone: these assertions are about a wall-clock hour, so they must not quietly
    re-read as UTC if the default ever changes."""
    monkeypatch.setenv("TIMEZONE", "Europe/Vienna")


def test_the_scan_hour_is_a_wall_clock_hour_and_does_not_drift_with_dst(vienna):
    """`run_hour = 7` is "seven in the morning", in January and in July alike.

    Resolved on a UTC clock it was seven UTC -- nine in Vienna in summer, eight in winter -- so
    the scan moved by an hour twice a year, on a form whose only label was "Hour".
    """
    schedule = _Schedule(run_hour=7)

    winter = slot_today(schedule, datetime(2026, 1, 15, 3, 0, tzinfo=UTC))
    summer = slot_today(schedule, datetime(2026, 7, 15, 3, 0, tzinfo=UTC))

    assert winter.astimezone(ZoneInfo("Europe/Vienna")).hour == 7
    assert summer.astimezone(ZoneInfo("Europe/Vienna")).hour == 7
    # The same wall-clock hour is a different instant on either side of the change.
    assert (winter.hour, summer.hour) == (6, 5), "the slot did not follow the offset"


def test_a_missed_schedule_slot_runs_once_when_the_runner_returns(vienna):
    schedule = _Schedule(run_hour=7)
    # September in Vienna is UTC+2, so the 07:00 slot falls at 05:00 UTC.
    now = datetime(2026, 9, 8, 11, 0, tzinfo=UTC)
    # Runner was down over the 07:00 slot: it should run now...
    assert is_scheduled_run_due(schedule, now, datetime(2026, 9, 7, 5, 0, tzinfo=UTC)) is True
    # ...but exactly once, not repeatedly to "catch up".
    assert is_scheduled_run_due(schedule, now, datetime(2026, 9, 8, 5, 30, tzinfo=UTC)) is False


def test_schedule_does_not_fire_before_its_slot(vienna):
    schedule = _Schedule(run_hour=7)
    before = datetime(2026, 9, 8, 4, 59, tzinfo=UTC)  # 06:59 in Vienna
    assert is_scheduled_run_due(schedule, before, None) is False


def test_disabled_schedule_never_fires():
    schedule = _Schedule(enabled=False)
    now = datetime.now(UTC) + timedelta(hours=1)
    assert is_scheduled_run_due(schedule, now, None) is False


def test_the_image_prewarms_the_model_the_code_actually_loads():
    """The Dockerfile must import the model name, never spell it out again.

    It did spell it out, and the two drifted: the image cached
    intfloat/multilingual-e5-small while the app loads
    paraphrase-multilingual-MiniLM-L12-v2. The layer exists precisely to stop the runner
    downloading a model on first scan, so a mismatch defeats it silently -- and stayed silent
    until a fastembed release dropped the stale name and failed the build instead.
    """
    from pathlib import Path

    from trouveur.ingest.embed.local import LocalOnnxMpnetProvider, LocalOnnxProvider

    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text("utf-8")
    # Comments may name the old model to explain the trap; only the instructions must not.
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    prewarm = instructions.split("FASTEMBED_CACHE_PATH", 1)[1]
    assert "from trouveur.ingest.embed.local import" in prewarm
    assert "model_name=p.model" in prewarm
    # Every local provider, not only the configured one: the model is chosen by a setting, so an
    # image carrying just today's choice turns a model switch into a runtime download.
    for provider in (LocalOnnxProvider, LocalOnnxMpnetProvider):
        assert provider.__name__ in prewarm
        assert provider.model not in instructions
    assert "intfloat/" not in instructions


class _FakeSource:
    requires_detail = False

    def __init__(self, name: str, swept: list[str]) -> None:
        self.name = name
        self._swept = swept

    async def sweep(self, client, sink, *, backfill=False):
        from trouveur.sources.base import SweepOutcome

        self._swept.append(self.name)
        # A complete sweep: the thing a cancelled run must never be able to report.
        return SweepOutcome(closable_scopes=[self.name], documents=1)

    async def fetch_detail(self, client, external_id):
        return None


async def test_a_cancelled_run_stops_before_the_next_source(monkeypatch):
    """Cancellation is checked between sources, so the one in flight still finishes cleanly."""
    from trouveur.ingest import pipeline

    swept: list[str] = []
    sources = [_FakeSource(name, swept) for name in ("first", "second", "third")]

    async def fake_sweep_one(client, source, settings, backfill):
        await source.sweep(client, None, backfill=backfill)
        return pipeline.SourceReport(documents=1, complete=True)

    monkeypatch.setattr(pipeline, "_sweep_source", fake_sweep_one)

    async def cancelled() -> bool:
        # Not cancelled until the first source has been swept.
        return bool(swept)

    report = await pipeline.sweep_sources(
        None, sources, settings=None, control=pipeline.RunControl(cancelled=cancelled)
    )

    assert swept == ["first"]
    assert report.cancelled
    assert report.skipped == ["second", "third"]
    assert "cancelled before second, third" in report.summary()


async def test_an_uncancelled_run_sweeps_every_source(monkeypatch):
    """The guard above must not be able to pass by simply never sweeping anything."""
    from trouveur.ingest import pipeline

    swept: list[str] = []
    sources = [_FakeSource(name, swept) for name in ("first", "second", "third")]

    async def fake_sweep_one(client, source, settings, backfill):
        await source.sweep(client, None, backfill=backfill)
        return pipeline.SourceReport(documents=1, complete=True)

    monkeypatch.setattr(pipeline, "_sweep_source", fake_sweep_one)

    report = await pipeline.sweep_sources(None, sources, settings=None)
    assert swept == ["first", "second", "third"]
    assert not report.cancelled and report.skipped == []


async def test_progress_is_reported_per_source_with_the_one_coming_next(monkeypatch):
    """The panel names the source being fetched, so the callback must hand over the next one."""
    from trouveur.ingest import pipeline

    sources = [_FakeSource(name, []) for name in ("first", "second")]
    seen: list[tuple[int, str | None]] = []
    totals: list[int] = []

    async def fake_sweep_one(client, source, settings, backfill):
        return pipeline.SourceReport(documents=1)

    monkeypatch.setattr(pipeline, "_sweep_source", fake_sweep_one)

    await pipeline.sweep_sources(
        None,
        sources,
        settings=None,
        control=pipeline.RunControl(
            starting=lambda total: totals.append(total) or _noop(),
            source_done=lambda done, nxt: seen.append((done, nxt)) or _noop(),
        ),
    )
    assert totals == [2]
    assert seen == [(1, "second"), (2, None)]


async def _noop() -> None:
    return None


async def test_a_match_only_run_sweeps_nothing_and_matches_one_user(monkeypatch):
    """The run named a user, so ingest is skipped entirely: no source is touched, no digest sent."""
    from types import SimpleNamespace

    from trouveur.match.pipeline import MatchReport
    from trouveur.runner import service

    calls: list[str] = []

    async def fake_ingest_run(**kwargs):
        calls.append("ingest")
        raise AssertionError("a match-only run must not sweep")

    async def fake_run_for_user(user_id, settings):
        calls.append(f"match:{user_id}")
        return MatchReport(user_id=user_id, retrieved=3, scored=2)

    finished: dict = {}

    async def fake_finish_run(conn, run_id, *, status, report=None, error=None):
        finished.update(run_id=run_id, status=status, report=report, error=error)

    class _Conn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(service.ingest, "run", fake_ingest_run)
    monkeypatch.setattr(service.matching, "run_for_user", fake_run_for_user)
    monkeypatch.setattr(service.admin_q, "finish_run", fake_finish_run)
    monkeypatch.setattr(service, "connect", lambda: _Conn())

    run = SimpleNamespace(id=7, match_user_id=42, only_source=None, backfill=False)
    await service._execute(None, run)

    assert calls == ["match:42"]
    assert finished["run_id"] == 7 and finished["status"] == service.RunStatus.SUCCESS
    assert finished["report"]["matches"][0]["scored"] == 2



async def test_detail_fetches_run_alongside_embedding_rather_than_in_front_of_it():
    """Detail is network-bound and mostly asleep; in series it left the cores idle.

    A measured tick with Workday's queue full was 135s, 40s of which was detail waiting on the
    shared per-provider interval while nothing encoded. Here detail waits for a signal only the
    embed half can send, so the old serial order cannot complete it: the failure is a timeout
    rather than a number that happens to look wrong.
    """
    import asyncio

    from trouveur.runner import service

    embedding_reached = asyncio.Event()

    async def fake_drain_detail(conn, client, sources, *, limit):
        await asyncio.wait_for(embedding_reached.wait(), timeout=5)
        return 7

    async def fake_drain_embed(conn, *, limit):
        embedding_reached.set()
        return 11

    done = await _run_drain(
        service, detail=fake_drain_detail, embed=fake_drain_embed
    )
    assert done["detail"] == 7
    assert done["embed"] == 11


async def test_a_failing_detail_half_no_longer_stops_embedding():
    """One stage's exception used to stop all four, silently, for hours.

    A Workday external id raised ValueError out of drain_detail; `drain_queues` had no isolation,
    so the tick died before derivation, dedupe or embedding ran. The embed queue sat still while
    the log showed only a generic tick failure every 20 seconds.
    """
    from trouveur.runner import service

    async def exploding_detail(conn, client, sources, *, limit):
        raise ValueError("not enough values to unpack (expected 3, got 1)")

    async def fake_drain_embed(conn, *, limit):
        return 11

    done = await _run_drain(
        service, detail=exploding_detail, embed=fake_drain_embed
    )
    assert done["detail"] == 0, "the failing half must report no work, not crash the tick"
    assert done["embed"] == 11, "embedding must still run when detail fails"


async def _run_drain(service, *, detail, embed):
    """Drive drain_queues with every seam stubbed: no database, no network, no model."""
    from types import SimpleNamespace

    import pytest as _pytest

    class _Conn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_enabled_tenants(conn):
        return {}

    async def fake_drain_derive(conn):
        return 3

    async def fake_drain_dedup(conn):
        return 5

    monkeypatch = _pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(service, "connect", lambda: _Conn())
        monkeypatch.setattr(service, "PoliteClient", lambda: _Client())
        monkeypatch.setattr(service, "build_sources", lambda **kwargs: [])
        monkeypatch.setattr(service.admin_q, "enabled_tenants", fake_enabled_tenants)
        monkeypatch.setattr(service.workers, "drain_detail", detail)
        monkeypatch.setattr(service.workers, "drain_derive", fake_drain_derive)
        monkeypatch.setattr(service.workers, "drain_dedup", fake_drain_dedup)
        monkeypatch.setattr(service.workers, "drain_embed", embed)
        return await service.drain_queues(SimpleNamespace(embed_batch_size=128))
    finally:
        monkeypatch.undo()
