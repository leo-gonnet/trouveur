"""The freshness horizon, against a real corpus.

A job radar that surfaces a vacancy a fortnight late has, for anything competitive, found
nothing. The horizon is how that is enforced, and it is enforced in four places that have to
agree: what retrieval will return, what the queue considers worth embedding, what the pruner
removes, and what the dashboard measures coverage against.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.integration.seed import seed_corpus, seed_user
from trouveur.db.engine import connect
from trouveur.db.queries import jobs as jobs_q
from trouveur.db.queries import match as mq

pytestmark = pytest.mark.asyncio

NOW = datetime.now(UTC)
HORIZON = timedelta(days=7)
# These tests are about how old an ADVERT may be. The candidate window -- how recently we got it
# -- is opened to the whole horizon so it cannot be what makes an assertion pass.
SINCE = NOW - HORIZON


async def _age_everything(days: int) -> None:
    """Backdate the whole seeded corpus, both dates the horizon reads."""
    async with connect() as conn:
        await conn.exec_driver_sql(
            f"UPDATE job SET posted_at = now() - interval '{days} days', "
            f"first_seen_at = now() - interval '{days} days'"
        )


async def test_a_posting_past_the_horizon_is_not_retrieved(
    clean_db, gh_board, aa_listing, aa_detail
):
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, profile = await seed_user(title="Process Engineer")

    # Set the age rather than assume it: the fixtures carry fixed publication dates that are
    # already weeks in the past and get worse every month this repository exists.
    await _age_everything(1)
    async with connect() as conn:
        fresh = await mq.lexical_candidates(conn, profile, "ingenieur", 10, SINCE, SINCE)
    assert fresh, "the seeded corpus should be retrievable before it is aged"

    await _age_everything(30)
    async with connect() as conn:
        stale = await mq.lexical_candidates(conn, profile, "ingenieur", 10, SINCE, SINCE)
    assert stale == [], "a posting a month old was still returned"


async def test_a_posting_with_no_publication_date_falls_back_to_when_we_saw_it(
    clean_db, gh_board, aa_listing, aa_detail
):
    """A source that stops sending dates must not vanish, and must not become immortal either."""
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, profile = await seed_user(title="Process Engineer")

    await _age_everything(1)
    async with connect() as conn:
        await conn.exec_driver_sql("UPDATE job SET posted_at = NULL")
        found = await mq.lexical_candidates(conn, profile, "ingenieur", 10, SINCE, SINCE)
    assert found, "an undated posting seen today should still be recommended"

    await _age_everything(30)
    async with connect() as conn:
        await conn.exec_driver_sql("UPDATE job SET posted_at = NULL")
        gone = await mq.lexical_candidates(conn, profile, "ingenieur", 10, SINCE, SINCE)
    assert gone == [], "an undated posting never aged out; first_seen_at was ignored"


async def test_the_pruner_drops_vectors_for_postings_that_aged_out(
    clean_db, gh_board, aa_listing, aa_detail
):
    await seed_corpus(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        before = (
            await conn.exec_driver_sql("SELECT count(*) FROM job_embedding")
        ).scalar()
    assert before > 0, "nothing was embedded; this test would pass over an empty set"

    await _age_everything(30)
    async with connect() as conn:
        removed = await jobs_q.prune_stale_embeddings(conn, NOW - HORIZON)
        after = (await conn.exec_driver_sql("SELECT count(*) FROM job_embedding")).scalar()
    assert removed == before
    assert after == 0


async def test_the_pruner_leaves_fresh_vectors_alone(clean_db, gh_board, aa_listing, aa_detail):
    await seed_corpus(gh_board, aa_listing, aa_detail)
    await _age_everything(1)
    async with connect() as conn:
        before = (
            await conn.exec_driver_sql("SELECT count(*) FROM job_embedding")
        ).scalar()
        removed = await jobs_q.prune_stale_embeddings(conn, NOW - HORIZON)
        after = (await conn.exec_driver_sql("SELECT count(*) FROM job_embedding")).scalar()
    assert removed == 0
    assert after == before


async def test_the_queue_does_not_re_embed_what_the_pruner_just_removed(
    clean_db, gh_board, aa_listing, aa_detail
):
    """Without the horizon in the refill query this is an infinite loop, not a slow one."""
    from trouveur.ingest.embed import embedding_version
    from trouveur.work import WorkKind

    await seed_corpus(gh_board, aa_listing, aa_detail)
    await _age_everything(30)
    async with connect() as conn:
        await jobs_q.prune_stale_embeddings(conn, NOW - HORIZON)
        await conn.exec_driver_sql("DELETE FROM work_item WHERE kind = 'embed'")

    # An explicit horizon, not the configured one: conftest widens the setting so that fixtures
    # with fixed dates stay usable, and reading it here would hand this test a cutoff in 1926
    # under which nothing is ever stale -- so it would pass without testing anything.
    from trouveur.work.queue import _stale_query

    async with connect() as conn:
        stale = list(
            await conn.execute(
                _stale_query(WorkKind.EMBED, embedding_version(), 0, 100, NOW - HORIZON)
            )
        )
    assert stale == [], "the refill re-queued postings the pruner had just dropped"


async def test_a_daily_run_sees_only_what_the_last_sweep_added(
    clean_db, gh_board, aa_listing, aa_detail
):
    """A posting gets one chance, on the day it arrives.

    With a week-wide candidate window a posting that had been beaten by two thousand others for
    six days running would surface on the seventh, because the week went quiet -- nothing about
    it had changed except the competition.
    """
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, profile = await seed_user(title="Process Engineer")
    await _age_everything(2)

    narrow = NOW - timedelta(hours=25)
    async with connect() as conn:
        assert await mq.lexical_candidates(conn, profile, "ingenieur", 10, SINCE, narrow) == []
        # The same corpus, the window a profile change uses: still retrievable, which is what
        # makes a profile change able to look back over the week.
        assert await mq.lexical_candidates(conn, profile, "ingenieur", 10, SINCE, SINCE)


async def test_a_new_arrival_is_when_we_got_it_not_when_it_was_posted(
    clean_db, gh_board, aa_listing, aa_detail
):
    """The candidate window reads first_seen_at, and the staleness bound reads COALESCE.

    Boards routinely list a posting with a publication date days before we discover it. Folding
    the candidate window into the same COALESCE would drop those on the one run that could ever
    have offered them -- silently, since they would simply not be retrieved.
    """
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, profile = await seed_user(title="Process Engineer")

    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE job SET posted_at = now() - interval '3 days', first_seen_at = now()"
        )
        found = await mq.lexical_candidates(
            conn, profile, "ingenieur", 10, SINCE, NOW - timedelta(hours=25)
        )
    assert found, "a posting published three days ago but first seen today is a new arrival"
