"""End-to-end exercise of the ingest and match paths against a real Postgres.

Writing these found two failures nothing else could have: a query that bundled `SET LOCAL` with a
SELECT (asyncpg runs parameterised queries as prepared statements and rejects two commands), and an
f-string prefix dropped from _DENSE_SQL so a literal "{_ELIGIBLE}" reached the server.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.integration.seed import seed_corpus, seed_user
from trouveur import clock

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
        to_score = await mq.pending_rerank(conn, user_id, profile.version)
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
        # Everything one run scores is published as one edition, on the day it ran.
        today = clock.today()
        await mq.publish_edition(
            conn,
            [{"user_id": user_id, "day": today, "job_id": row.job_id,
              "profile_version": profile.version, "llm_score": 88, "llm_reason": "good",
              "llm_red_flags": ["none"]} for row in to_score],
        )
        days = await mq.editions(conn, user_id)
        assert len(days) == 1
        assert len(await mq.edition(conn, user_id, days[0].day)) == len(to_score)


async def test_bumping_a_version_refills_the_queue(clean_db, gh_board, aa_listing, aa_detail):
    """The upgrade path. Bumping a version must make the whole corpus eligible again."""
    from trouveur import versions
    from trouveur.db.engine import connect
    from trouveur.work import WorkKind, refill

    await seed_corpus(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        queued, _ = await refill(conn, WorkKind.DERIVE, str(versions.DERIVE_VERSION + 1))
    assert queued == 3


async def _credentialled_user(monkeypatch, *, budget="5"):
    """A user with matches waiting to be scored and a key to score them with."""
    from trouveur.crypto import encrypt
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as mq
    from trouveur.db.queries import users as uq

    monkeypatch.setenv("ENCRYPTION_KEY", "a-test-encryption-secret")
    user_id, profile = await seed_user(title="Process Engineer")
    async with connect() as conn:
        await uq.save_credential(
            conn, user_id,
            api_key_encrypted=encrypt("sk-test"), api_key_fingerprint="sk-…test",
            model="test/model", provider_pin=None, monthly_budget_usd=Decimal(budget),
        )
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
        credential = await uq.get_credential(conn, user_id)
    return user_id, profile, credential, job_ids


async def test_one_failed_call_does_not_lose_the_rest_of_the_wave(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """At a few thousand calls a run, ending the stage on the first timeout would leave most of
    the corpus unscored. The posting that failed stays pending -- never scored, never cached."""
    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as mq
    from trouveur.match import llm, pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile, credential, job_ids = await _credentialled_user(monkeypatch)
    doomed = job_ids[0]

    async def score_one(settings, prof, candidate, **kwargs):
        if candidate.job_id == doomed:
            raise llm.LlmError("the model request could not be completed")
        return (
            rerank._Score(score=77, reason="fine"),
            llm.Usage(tokens_in=500, tokens_out=40, cost_usd=Decimal("0.0001")),
        )

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        scored = await pipeline._score_pending(
            conn, get_settings(), profile, credential, report
        )
        still_pending = await mq.pending_rerank(conn, user_id, profile.version)

    assert report.scored == len(job_ids) - 1
    assert {row["job_id"] for row in scored} == set(job_ids) - {doomed}
    assert [row.job_id for row in still_pending] == [doomed]
    assert report.errors and "1 scoring call(s) failed" in report.errors[0]
    # Billed for what was sent, aggregated per wave rather than per posting.
    assert report.cost_usd == Decimal("0.0001") * (len(job_ids) - 1)


async def test_an_unparseable_response_leaves_a_posting_unscored_never_zero(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """Caching a zero would hide a good job permanently; leaving it pending retries it."""
    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as mq
    from trouveur.match import llm, pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile, credential, job_ids = await _credentialled_user(monkeypatch)

    async def score_one(settings, prof, candidate, **kwargs):
        return None, llm.Usage(tokens_in=500, tokens_out=40, cost_usd=Decimal("0.0001"))

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        assert await pipeline._score_pending(
            conn, get_settings(), profile, credential, report
        ) == []
        # Billed, because the call was made -- but nothing cached, so the next run asks again.
        pending = await mq.pending_rerank(conn, user_id, profile.version)
        assert len(pending) == len(job_ids)
        assert await mq.cached_scores(
            conn, user_id, profile.version, [row.content_hash for row in pending]
        ) == {}
    assert report.scored == 0
    assert report.cost_usd > 0


async def test_the_ceiling_stops_the_run_before_the_wave_is_sent(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """Checked before the money is spent, not reported after it. A zero ceiling must send nothing
    at all -- a retry loop on someone else's card is not something to discover from the user."""
    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.match import pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile, credential, _ = await _credentialled_user(monkeypatch, budget="0")

    calls = []

    async def score_one(settings, prof, candidate, **kwargs):
        calls.append(candidate.job_id)
        raise AssertionError("no call may be issued once the ceiling is reached")

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        assert await pipeline._score_pending(
            conn, get_settings(), profile, credential, report
        ) == []
    assert calls == []
    assert report.stopped_on_budget is True
