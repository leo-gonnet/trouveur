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
        assert await mq.lexical_candidates(conn, profile, "ingenieur", 10, _ANY_AGE, _ANY_AGE)
        assert await mq.lexical_candidates(conn, profile, "munchen", 10, _ANY_AGE, _ANY_AGE)
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
        assert await mq.dense_candidates(conn, profile, vector, 10, _ANY_AGE, _ANY_AGE)


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
        to_score = await mq.pending_rerank(conn, user_id, profile.version, job_ids)
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
        rows = await mq.edition(
            conn, user_id, days[0].day, days[0].profile_version, limit=100
        )
        assert len(rows) == len(to_score)


async def test_bumping_a_version_refills_the_queue(clean_db, gh_board, aa_listing, aa_detail):
    """The upgrade path. Bumping a version must make the whole corpus eligible again."""
    from trouveur import versions
    from trouveur.db.engine import connect
    from trouveur.work import WorkKind, refill

    await seed_corpus(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        queued, _ = await refill(conn, WorkKind.DERIVE, str(versions.DERIVE_VERSION + 1))
    assert queued == 3


async def _funded_user(*, credit="5", ceiling="5", admin=False):
    """A user with matches waiting to be scored and the means to pay for scoring them.

    `credit` and `ceiling` are separate on purpose: they are the two independent limits, and a test
    that could only move both together could not tell which one stopped a run.
    """
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as mq
    from trouveur.db.queries import users as uq

    user_id, profile = await seed_user(title="Process Engineer")
    # The ceiling is read off the profile the scorer is handed, so it is set here rather than
    # written and read back: the column's round trip is test_queries_execute's job.
    profile = profile.model_copy(update={"daily_ceiling_usd": Decimal(ceiling)})
    async with connect() as conn:
        if Decimal(credit) > 0:
            await uq.grant_credit(
                conn, user_id, amount_usd=Decimal(credit), granted_by=user_id, note="test"
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
    from trouveur.match.pipeline import Spending

    # What `_allowance` would have returned. Built rather than called, because these tests exercise
    # the scorer and not the gate in front of it.
    return user_id, profile, Spending(api_key="sk-test", unlimited_credit=admin), job_ids


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
    user_id, profile, spending, job_ids = await _funded_user()
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
            conn, get_settings(), profile, spending, report, job_ids
        )
        still_pending = await mq.pending_rerank(conn, user_id, profile.version, job_ids)

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
    user_id, profile, spending, job_ids = await _funded_user()

    async def score_one(settings, prof, candidate, **kwargs):
        return None, llm.Usage(tokens_in=500, tokens_out=40, cost_usd=Decimal("0.0001"))

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        assert await pipeline._score_pending(
            conn, get_settings(), profile, spending, report, job_ids
        ) == []
        # Billed, because the call was made -- but nothing cached, so the next run asks again.
        pending = await mq.pending_rerank(conn, user_id, profile.version, job_ids)
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
    user_id, profile, spending, job_ids = await _funded_user(ceiling="0")

    calls = []

    async def score_one(settings, prof, candidate, **kwargs):
        calls.append(candidate.job_id)
        raise AssertionError("no call may be issued once the ceiling is reached")

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        assert await pipeline._score_pending(
            conn, get_settings(), profile, spending, report, job_ids
        ) == []
    assert calls == []
    assert report.stopped_on_ceiling is True


async def test_scoring_stops_when_two_blocks_in_a_row_score_badly(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """What decides how long an edition is: the scorer running out of good postings.

    Not a rank cut, which is arbitrary, and not a score threshold, which hides postings the reader
    already paid for. Everything scored is still published whatever it scored -- the rule only
    decides when to stop buying.
    """
    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as mq
    from trouveur.match import llm, pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile, spending, job_ids = await _funded_user()
    monkeypatch.setattr(rerank, "BLOCK", 1)
    verdicts = iter([10, 10, 95])
    calls = []

    async def score_one(settings, prof, candidate, **kwargs):
        calls.append(candidate.job_id)
        return (
            rerank._Score(score=next(verdicts), reason="x"),
            llm.Usage(tokens_in=500, tokens_out=40, cost_usd=Decimal("0.0001")),
        )

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        scored = await pipeline._score_pending(
            conn, get_settings(), profile, spending, report, job_ids
        )
        still_pending = await mq.pending_rerank(conn, user_id, profile.version, job_ids)

    assert len(calls) == 2, "the third block was bought after the scores had run out"
    assert report.stopped_on_scores is True
    # Published anyway: the rule stops spending, it does not hide what was spent.
    assert len(scored) == 2
    assert len(still_pending) == 1


async def test_one_good_block_resets_the_patience(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """Two blocks in a row, not two blocks in total.

    Retrieval order correlates only loosely with the model's verdict, so a single weak block is
    noise. Counting them cumulatively would cut a rich day short on the strength of two bad
    postings that happened to be spread across it.
    """
    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.match import llm, pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile, spending, job_ids = await _funded_user()
    monkeypatch.setattr(rerank, "BLOCK", 1)
    verdicts = iter([10, 95, 10])
    calls = []

    async def score_one(settings, prof, candidate, **kwargs):
        calls.append(candidate.job_id)
        return (
            rerank._Score(score=next(verdicts), reason="x"),
            llm.Usage(tokens_in=500, tokens_out=40, cost_usd=Decimal("0.0001")),
        )

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        await pipeline._score_pending(
            conn, get_settings(), profile, spending, report, job_ids
        )

    assert len(calls) == len(job_ids), "a good block did not reset the patience"
    assert report.stopped_on_scores is False


async def test_a_user_with_no_credit_buys_nothing_at_all(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """The gate that replaced "has this user a key".

    Asserted over the WHOLE run rather than over the scorer, because the expansion stage is billed
    too: gating the rerank alone would still bill three calls per profile version to a user who has
    been granted nothing. Retrieval is free and must still have run.
    """
    from trouveur.match import expand, pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, _, _, _ = await _funded_user(credit="0")

    calls: list[str] = []

    async def refuse(*args, **kwargs):
        calls.append("paid")
        raise AssertionError("no paid call may be made for a user with no credit")

    monkeypatch.setattr(rerank, "score_one", refuse)
    monkeypatch.setattr(expand, "expand_with_model", refuse)
    monkeypatch.setattr(expand, "expand_adverts", refuse)
    monkeypatch.setattr(expand, "summarise_background", refuse)

    report = await pipeline.run_for_user(user_id, whole_horizon=True)

    assert calls == []
    assert report.cost_usd == 0
    assert report.scored == 0
    assert report.retrieved > 0, "retrieval is free and must run regardless"


async def test_an_admin_needs_no_credit_but_is_still_capped_by_their_own_ceiling(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """An admin grants credit and has none of their own, so the balance cannot apply to them. The
    daily ceiling still does: an unlimited balance is not a reason to be unmetered."""
    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.match import llm, pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile, spending, job_ids = await _funded_user(
        credit="0", ceiling="5", admin=True
    )
    assert spending.unlimited_credit is True

    async def score_one(settings, prof, candidate, **kwargs):
        return (
            rerank._Score(score=80, reason="x"),
            llm.Usage(tokens_in=500, tokens_out=40, cost_usd=Decimal("0.0001")),
        )

    monkeypatch.setattr(rerank, "score_one", score_one)

    # The ceiling first, while nothing has been scored yet: a run that scored everything would
    # leave nothing pending, and the second half would pass by having nothing to buy.
    capped = profile.model_copy(update={"daily_ceiling_usd": Decimal(0)})
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        assert await pipeline._score_pending(
            conn, get_settings(), capped, spending, report, job_ids
        ) == []
    assert report.stopped_on_ceiling is True, "an admin's own ceiling did not apply"
    assert report.stopped_on_credit is False, "credit cannot stop an admin who needs none"

    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        scored = await pipeline._score_pending(
            conn, get_settings(), profile, spending, report, job_ids
        )
    assert len(scored) == len(job_ids), "an admin was stopped by a balance they do not need"
    assert report.stopped_on_credit is False


async def test_credit_runs_out_before_the_ceiling_does_and_says_which(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """Two limits, and the run must report the one that actually stopped it: a reader whose ceiling
    was reached waits for tomorrow, one whose credit is gone has to ask somebody."""
    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.match import llm, pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    # Credit far below the ceiling, so the ceiling cannot be what bites. Enough for the first call
    # and not the second, so the run gets past the gate and then runs out.
    user_id, profile, spending, job_ids = await _funded_user(credit="0.05", ceiling="5")
    monkeypatch.setattr(rerank, "BLOCK", 1)

    async def score_one(settings, prof, candidate, **kwargs):
        return (
            rerank._Score(score=90, reason="x"),
            llm.Usage(tokens_in=500, tokens_out=40, cost_usd=Decimal("0.03")),
        )

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        scored = await pipeline._score_pending(
            conn, get_settings(), profile, spending, report, job_ids
        )
    assert report.stopped_on_credit is True
    assert report.stopped_on_ceiling is False
    # The first call was affordable; five cents did not stretch to a second at three cents each.
    assert 0 < len(scored) < len(job_ids)


async def test_a_ceiling_counts_today_only_and_not_what_was_spent_yesterday(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """A DAILY ceiling tested against a running total would never lift. Spend is keyed on the day,
    so yesterday's rows cannot hold today's run back."""
    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.db.schema import user_llm_spend
    from trouveur.match import llm, pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile, spending, job_ids = await _funded_user(credit="5", ceiling="0.10")

    async with connect() as conn:
        # Yesterday, the whole ceiling was spent.
        await conn.execute(
            user_llm_spend.insert().values(
                user_id=user_id,
                period_day=clock.today() - timedelta(days=1),
                tokens_in=1000, tokens_out=100, cost_usd=Decimal("0.10"), calls=50,
            )
        )

    async def score_one(settings, prof, candidate, **kwargs):
        return (
            rerank._Score(score=85, reason="x"),
            llm.Usage(tokens_in=500, tokens_out=40, cost_usd=Decimal("0.0001")),
        )

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        scored = await pipeline._score_pending(
            conn, get_settings(), profile, spending, report, job_ids
        )
    assert len(scored) == len(job_ids), "yesterday's spend was counted against today's ceiling"
    assert report.stopped_on_ceiling is False


async def test_a_balance_is_granted_minus_spent_and_a_grant_never_overwrites_one(clean_db):
    """The balance is derived, so it cannot drift from what was billed, and a top-up is a row --
    two of them add up rather than the second replacing the first."""
    from trouveur.db.engine import connect
    from trouveur.db.queries import users as uq

    user_id, _ = await seed_user()
    async with connect() as conn:
        assert (await uq.credit(conn, user_id)).balance_usd == 0

        await uq.grant_credit(
            conn, user_id, amount_usd=Decimal("5.00"), granted_by=user_id, note="first"
        )
        await uq.grant_credit(
            conn, user_id, amount_usd=Decimal("2.50"), granted_by=user_id, note="second"
        )
        await uq.add_spend(
            conn, user_id, tokens_in=100, tokens_out=10, cost_usd=Decimal("1.25")
        )

        credit = await uq.credit(conn, user_id)
        grants = await uq.credit_grants(conn, user_id)

    assert credit.granted_usd == Decimal("7.50"), "the second grant replaced the first"
    assert credit.spent_usd == Decimal("1.25")
    assert credit.balance_usd == Decimal("6.25")
    assert len(grants) == 2, "a grant must stay on the record"


async def test_spend_from_elsewhere_is_seen_between_blocks_not_only_at_the_start(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """What is left has to be re-read from the database, never decremented from a snapshot.

    This process is not the only thing that can spend a user's credit: the expansion stage already
    did, and a match-only run claimed by a second runner with SKIP LOCKED is meant to proceed in
    parallel. Against a process-local belief, two concurrent runs would each authorise a full day's
    ceiling. Simulated here by spending out of band while the scorer is between blocks.
    """
    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.db.queries import users as uq
    from trouveur.match import llm, pipeline, rerank

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile, spending, job_ids = await _funded_user(credit="5", ceiling="1.00")
    monkeypatch.setattr(rerank, "BLOCK", 1)
    calls = []

    async def score_one(settings, prof, candidate, **kwargs):
        calls.append(candidate.job_id)
        # Somebody else's run bills the rest of this user's ceiling while we are mid-block.
        if len(calls) == 1:
            async with connect() as other:
                await uq.add_spend(
                    other, user_id, tokens_in=1, tokens_out=1, cost_usd=Decimal("1.00")
                )
        return (
            rerank._Score(score=90, reason="x"),
            llm.Usage(tokens_in=500, tokens_out=40, cost_usd=Decimal("0.0001")),
        )

    monkeypatch.setattr(rerank, "score_one", score_one)
    report = pipeline.MatchReport(user_id=user_id)
    async with connect() as conn:
        await pipeline._score_pending(
            conn, get_settings(), profile, spending, report, job_ids
        )

    assert len(calls) == 1, "the ceiling was tested against a stale in-process snapshot"
    assert report.stopped_on_ceiling is True


async def test_a_board_that_keeps_failing_sorts_above_the_healthy_ones(clean_db):
    """The failure streak is the only signal a dead board gives, and it has to be findable.

    Nothing disables a board automatically -- a run of failures is as likely to be a provider
    outage as a 404 -- so the streak IS the alert. Ordered alphabetically it was not one: a
    Greenhouse slug that had 404ed for weeks sat in the middle of 211 identical-looking rows.
    """
    from trouveur.db.engine import connect
    from trouveur.db.queries import admin as admin_q
    from trouveur.sources.base import ScopeResult

    async with connect() as conn:
        await admin_q.add_tenants(conn, "greenhouse", ["aaa-healthy", "zzz-dead"])
        for _sweep in range(3):
            await admin_q.record_scope_health(
                conn,
                "greenhouse",
                [
                    ScopeResult(scope="aaa-healthy", ok=True, documents=12),
                    ScopeResult(scope="zzz-dead", ok=False, error="returned HTTP 404."),
                ],
            )
        rows = await admin_q.list_tenants(conn)
        failing = await admin_q.list_tenants(conn, failing_only=True)

    assert [row.scope for row in rows] == ["zzz-dead", "aaa-healthy"]
    assert rows[0].consecutive_failures == 3
    assert [row.scope for row in failing] == ["zzz-dead"]


async def test_a_scheduled_run_fails_and_records_why_when_the_key_is_rejected(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """A rejected OPENROUTER_API_KEY must not read as a healthy scan that happened to score none.

    The scheduled path finished every run SUCCESS and left `MatchReport.errors` out of the report
    altogether, so the only trace of a dead key was `scored: 0` -- which is exactly what a quiet
    night looks like. It cannot be judged on `retrieved` either: retrieval is free, needs no key
    and still returns hundreds of postings, which is how the match-only path's own check missed
    this too.
    """
    import httpx

    from trouveur.config import get_settings
    from trouveur.db.engine import connect
    from trouveur.db.queries import admin as admin_q
    from trouveur.ingest.pipeline import IngestReport
    from trouveur.match import llm
    from trouveur.models import RunStatus, RunTrigger
    from trouveur.runner import service

    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, _profile, _spending, _job_ids = await _funded_user()

    class RejectingClient:
        """OpenRouter answering 401, so `llm.complete` maps it exactly as production would."""

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            return httpx.Response(
                401,
                json={"error": {"message": "User not found."}},
                request=httpx.Request("POST", url),
            )

    async def no_sweep(**kwargs):
        return IngestReport()

    monkeypatch.setattr(llm.httpx, "AsyncClient", RejectingClient)
    monkeypatch.setattr(service.ingest, "run", no_sweep)

    async with connect() as conn:
        await admin_q.enqueue_run(conn, trigger=RunTrigger.SCHEDULED)
        run = await admin_q.claim_next_run(conn)
    await service._execute(get_settings(), run)
    async with connect() as conn:
        finished = (await admin_q.recent_runs(conn, limit=1))[0]

    assert finished.status == RunStatus.FAILED.value
    assert f"user {user_id}" in finished.error
    assert "OPENROUTER_API_KEY" in finished.error
    entry = finished.report["matches"][0]
    assert entry["errors"], "the report must carry what went wrong, not only the run's status"
    assert entry["scored"] == 0
    # The trap: free retrieval still succeeded, so a verdict keyed on it reads green.
    assert entry["retrieved"] > 0
