"""End-to-end exercise of every SQL path against a real Postgres.

Skipped unless TROUVEUR_TEST_DATABASE_URL is set, so the default suite stays offline and fast.
This is the deliberate exception to "no database in tests", and it earns it: SQL that compiles is
not SQL that runs. Writing this found two failures nothing else could have:

  - `SET LOCAL hnsw.ef_search = ...;` bundled with the dense SELECT, which asyncpg rejects because
    parameterised queries run as prepared statements and cannot carry two commands.
  - an f-string prefix dropped from _DENSE_SQL, shipping a literal "{_HARD_FILTERS}" to the server.

Run it with a throwaway database:

    podman run -d --rm --name pg -e POSTGRES_USER=trouveur -e POSTGRES_PASSWORD=x \\
        -e POSTGRES_DB=trouveur -p 55432:5432 pgvector/pgvector:pg16
    export TROUVEUR_TEST_DATABASE_URL=postgresql+asyncpg://trouveur:x@127.0.0.1:55432/trouveur
    DATABASE_URL=$TROUVEUR_TEST_DATABASE_URL uv run alembic upgrade head
    uv run pytest tests/test_integration_db.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

TEST_DB = os.environ.get("TROUVEUR_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TROUVEUR_TEST_DATABASE_URL is not set")

if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ.setdefault("EMBEDDING_PROVIDER", "deterministic")


@pytest.fixture
async def clean_db():
    """Truncate between tests, and rebuild the engine around each test's event loop.

    The engine is a module-level singleton whose pool binds to the loop that created it, and
    pytest-asyncio gives every test a fresh loop. Reusing it across tests fails with a pending-task
    error that has nothing to do with the code under test.
    """
    from trouveur.db import engine as engine_module
    from trouveur.db.engine import connect

    engine_module._engine = None
    async with connect() as conn:
        await conn.exec_driver_sql(
            """
            DO $$ DECLARE t text; BEGIN
              FOR t IN SELECT tablename FROM pg_tables
                       WHERE schemaname='public' AND tablename <> 'alembic_version'
              LOOP EXECUTE format('TRUNCATE %I CASCADE', t); END LOOP; END $$;
            """
        )
    yield
    if engine_module._engine is not None:
        await engine_module._engine.dispose()
        engine_module._engine = None


async def _seed(gh_board, aa_listing, aa_detail):
    """Ingest the fixtures the way the pipeline does, and drain every queue."""
    from trouveur.db.engine import connect
    from trouveur.db.queries import ingest as iq
    from trouveur.ingest import workers
    from trouveur.ingest.persist import persist
    from trouveur.models import DocumentKind, RawDocument
    from trouveur.sources.greenhouse import external_id

    docs = [
        RawDocument(
            source="greenhouse", external_id=external_id("beispiel", job["id"]),
            kind=DocumentKind.LISTING, scope="beispiel", payload=job,
        )
        for job in gh_board["jobs"]
    ]
    aa_id = aa_listing["referenznummer"]
    async with connect() as conn:
        await iq.archive_documents(conn, docs)
        await persist(conn, "greenhouse", [d.external_id for d in docs], requires_detail=False)
        await iq.archive_documents(
            conn,
            [RawDocument(source="arbeitsagentur", external_id=aa_id,
                         kind=DocumentKind.LISTING, payload=aa_listing)],
        )
        await persist(conn, "arbeitsagentur", [aa_id], requires_detail=True)
        await iq.archive_documents(
            conn,
            [RawDocument(source="arbeitsagentur", external_id=aa_id,
                         kind=DocumentKind.DETAIL, payload=aa_detail)],
        )
        await persist(conn, "arbeitsagentur", [aa_id], requires_detail=True)
    for drain in (workers.drain_derive, workers.drain_dedup, workers.drain_embed):
        async with connect() as conn:
            await drain(conn)
    return aa_id


async def _profile(**values):
    from trouveur.db.engine import connect
    from trouveur.db.queries import users as uq
    from trouveur.match.pipeline import profile_from_row

    async with connect() as conn:
        user_id = await uq.create_user(conn, "verifier", "argon2$fake", "v@example.test")
        await uq.save_profile(conn, user_id, {"countries": ["DE", "AT"], **values})
        return user_id, profile_from_row(await uq.get_profile(conn, user_id))


async def test_german_search_finds_a_compound_and_folds_umlauts(
    clean_db, gh_board, aa_listing, aa_detail
):
    """The two mandatory search guards, against the real indexes.

    'ingenieur' must find 'Wirtschaftsingenieur' (trigram, inside a compound) and 'munchen' must
    find 'München' (unaccent folding). Neither is expressible without a database, and the second
    failed for real: place names were missing from both search columns entirely, so city search
    -- the way people actually look for jobs -- returned nothing.
    """
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as mq

    await _seed(gh_board, aa_listing, aa_detail)
    user_id, profile = await _profile(title="Process Engineer")

    async with connect() as conn:
        assert await mq.lexical_candidates(conn, profile, "ingenieur", 10)
        assert await mq.lexical_candidates(conn, profile, "munchen", 10)
        assert await mq.search_jobs(conn, user_id, query="ingenieur")
        assert await mq.search_jobs(conn, user_id, query="munchen")


async def test_dense_retrieval_runs(clean_db, gh_board, aa_listing, aa_detail):
    """Guards the two failures above, both of which made every ANN query raise."""
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as mq
    from trouveur.ingest.embed import get_provider

    await _seed(gh_board, aa_listing, aa_detail)
    _, profile = await _profile(title="Process Engineer")
    vector = (await get_provider().embed_queries(["Prozessoptimierung"]))[0]

    async with connect() as conn:
        assert await mq.dense_candidates(conn, profile, vector, 10)


async def test_detail_arrival_changes_the_hash_and_a_replay_changes_nothing(
    clean_db, gh_board, aa_listing, aa_detail
):
    """Re-persisting unchanged content must enqueue no work.

    This is what stops a daily re-sweep re-deriving, re-embedding and re-scoring the whole corpus
    at the users' expense.
    """
    from trouveur.db.engine import connect
    from trouveur.ingest.persist import persist

    aa_id = await _seed(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        replay = await persist(conn, "arbeitsagentur", [aa_id], requires_detail=True)
    assert replay.changed == []
    assert replay.unchanged == 1


async def test_closing_a_posting_removes_its_embedding(
    clean_db, gh_board, aa_listing, aa_detail
):
    """job_embedding is the ANN index's population, so a closed posting must leave it.

    If it did not, the index would grow with all history instead of tracking the live corpus.
    """
    from trouveur.db.engine import connect
    from trouveur.db.queries import ingest as iq

    await _seed(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        before = (await conn.exec_driver_sql("SELECT count(*) FROM job_embedding")).scalar()
        closed = await iq.close_unseen(
            conn, "greenhouse", ["beispiel"], datetime.now(UTC) + timedelta(minutes=1)
        )
        after = (await conn.exec_driver_sql("SELECT count(*) FROM job_embedding")).scalar()
    assert closed == 2
    assert after == before - closed


async def test_a_delta_source_is_untouched_by_another_sources_close(
    clean_db, gh_board, aa_listing, aa_detail
):
    """Closing within a scope must not reach postings belonging to another source."""
    from trouveur.db.engine import connect
    from trouveur.db.queries import ingest as iq

    await _seed(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        await iq.close_unseen(
            conn, "greenhouse", ["beispiel"], datetime.now(UTC) + timedelta(minutes=1)
        )
        still_open = (
            await conn.exec_driver_sql(
                "SELECT count(*) FROM job WHERE source='arbeitsagentur' AND closed_at IS NULL"
            )
        ).scalar()
    assert still_open == 1


async def test_scores_are_cached_and_spend_accumulates(
    clean_db, gh_board, aa_listing, aa_detail
):
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as mq
    from trouveur.db.queries import users as uq

    await _seed(gh_board, aa_listing, aa_detail)
    user_id, profile = await _profile(title="Process Engineer")

    async with connect() as conn:
        job_ids = [
            row[0] for row in await conn.exec_driver_sql("SELECT id FROM job ORDER BY id")
        ]
        await mq.upsert_matches(
            conn,
            [
                {"user_id": user_id, "job_id": job_id, "profile_version": profile.version,
                 "retrieval_score": 1.0, "dense_rank": 1, "lexical_rank": None}
                for job_id in job_ids
            ],
        )
        pending = await mq.pending_rules(conn, user_id, 100)
        await mq.apply_rule_verdicts(
            conn,
            [{"user_id": user_id, "job_id": row.job_id, "rule_verdict": "pass",
              "rule_reason": "ok"} for row in pending],
        )
        to_score = await mq.pending_rerank(conn, user_id, profile.version, 100)
        await mq.apply_scores(
            conn, user_id,
            [{"job_id": row.job_id, "score": 88, "reason": "good", "red_flags": ["none"],
              "profile_version": profile.version} for row in to_score],
        )
        await mq.put_cached_scores(
            conn,
            [{"content_hash": row.content_hash, "user_id": user_id,
              "profile_version": profile.version, "score": 88, "reason": "good",
              "red_flags": ["none"], "model": "test"} for row in to_score],
        )
        cached = await mq.cached_scores(
            conn, user_id, profile.version, [row.content_hash for row in to_score]
        )
        assert len(cached) == len(to_score)

        total = await uq.add_spend(
            conn, user_id, tokens_in=100, tokens_out=20, cost_usd=Decimal("0.01")
        )
        total = await uq.add_spend(
            conn, user_id, tokens_in=100, tokens_out=20, cost_usd=Decimal("0.02")
        )
        assert total == Decimal("0.03")
        assert len(await mq.recommendations(conn, user_id, 70)) == len(to_score)


async def test_bumping_a_version_refills_the_queue(clean_db, gh_board, aa_listing, aa_detail):
    """The upgrade path. Bumping a version must make the whole corpus eligible again."""
    from trouveur import versions
    from trouveur.db.engine import connect
    from trouveur.work import WorkKind, refill

    await _seed(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        queued, _ = await refill(conn, WorkKind.DERIVE, str(versions.DERIVE_VERSION + 1))
    assert queued == 3
