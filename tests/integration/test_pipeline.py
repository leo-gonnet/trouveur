"""End-to-end exercise of the ingest and match paths against a real Postgres.

Writing these found two failures nothing else could have: a query that bundled `SET LOCAL` with a
SELECT (asyncpg runs parameterised queries as prepared statements and rejects two commands), and an
f-string prefix dropped from _DENSE_SQL so a literal "{_HARD_FILTERS}" reached the server.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.integration.seed import seed_corpus, seed_user

# The integration conftest neutralises the horizon so fixtures with fixed dates stay usable.
# Where a test is about retrieval rather than about freshness, it says so by passing this.
_ANY_AGE = datetime(2000, 1, 1, tzinfo=UTC)


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

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile = await seed_user(title="Process Engineer")

    async with connect() as conn:
        assert await mq.lexical_candidates(conn, profile, "ingenieur", 10, _ANY_AGE)
        assert await mq.lexical_candidates(conn, profile, "munchen", 10, _ANY_AGE)
        assert await mq.search_jobs(conn, user_id, query="ingenieur")
        assert await mq.search_jobs(conn, user_id, query="munchen")


async def test_dense_retrieval_runs(clean_db, gh_board, aa_listing, aa_detail):
    """Guards the two failures above, both of which made every ANN query raise."""
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as mq
    from trouveur.ingest.embed import get_provider

    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, profile = await seed_user(title="Process Engineer")
    vector = (await get_provider().embed_queries(["Prozessoptimierung"]))[0]

    async with connect() as conn:
        assert await mq.dense_candidates(conn, profile, vector, 10, _ANY_AGE)


async def test_detail_arrival_changes_the_hash_and_a_replay_changes_nothing(
    clean_db, gh_board, aa_listing, aa_detail
):
    """Re-persisting unchanged content must enqueue no work.

    This is what stops a daily re-sweep re-deriving, re-embedding and re-scoring the whole corpus
    at the users' expense.
    """
    from trouveur.db.engine import connect
    from trouveur.ingest.persist import persist

    aa_id = await seed_corpus(gh_board, aa_listing, aa_detail)
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

    await seed_corpus(gh_board, aa_listing, aa_detail)
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

    await seed_corpus(gh_board, aa_listing, aa_detail)
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

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile = await seed_user(title="Process Engineer")

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
        # Everything scored in one call lands in one edition, since scored_at is set by the
        # same statement that writes the score.
        days = await mq.editions(conn, user_id)
        assert len(days) == 1
        assert len(await mq.edition(conn, user_id, days[0].day, 70)) == len(to_score)


async def test_bumping_a_version_refills_the_queue(clean_db, gh_board, aa_listing, aa_detail):
    """The upgrade path. Bumping a version must make the whole corpus eligible again."""
    from trouveur import versions
    from trouveur.db.engine import connect
    from trouveur.work import WorkKind, refill

    await seed_corpus(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        queued, _ = await refill(conn, WorkKind.DERIVE, str(versions.DERIVE_VERSION + 1))
    assert queued == 3
