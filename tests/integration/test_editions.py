"""Recommendations published one day at a time.

The page used to be one list that only grew, ordered by score, so a strong posting from three
weeks ago outranked everything that arrived this morning and stayed there until it was
dismissed. An edition gives the page a time axis without a score cut: the day is the cut.

What has to hold for that to be worth anything: a day's edition is fixed once published, a day
that is missed leaves no hole, and nothing a user was shown becomes unreachable.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from trouveur.db.engine import connect
from trouveur.db.queries import match as mq

pytestmark = pytest.mark.asyncio


async def _rescore_on(user_id: int, job_ids: list[int], day: date, version: int = 1) -> None:
    """Put an existing match into a given edition, the way apply_scores would have."""
    async with connect() as conn:
        await conn.execute(
            sa.text(
                "UPDATE user_job_match SET llm_score = 80, scored_at = CAST(:day AS date), "
                "profile_version = :version "
                "WHERE user_id = :user AND job_id = ANY(CAST(:ids AS bigint[]))"
            ),
            {"day": day, "version": version, "user": user_id, "ids": job_ids},
        )


async def _all_match_ids(user_id: int) -> list[int]:
    """Every posting in the seeded corpus, matched to this user.

    The `seeded` fixture scores exactly one, which is enough to render a card and not enough to
    split across two days -- so the rest are matched here.
    """
    async with connect() as conn:
        jobs = await conn.execute(sa.text("SELECT id FROM job ORDER BY id"))
        job_ids = [row.id for row in jobs]
        await mq.upsert_matches(
            conn,
            [
                {
                    "user_id": user_id, "job_id": job_id, "profile_version": 1,
                    "retrieval_score": 1.0, "dense_rank": 1, "lexical_rank": 1,
                }
                for job_id in job_ids
            ],
        )
    return job_ids


async def test_postings_scored_on_different_days_land_in_different_editions(seeded):
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    assert len(ids) >= 2, "need at least two matches to split across two days"

    monday, tuesday = date(2026, 9, 14), date(2026, 9, 15)
    await _rescore_on(user_id, ids[:1], monday)
    await _rescore_on(user_id, ids[1:], tuesday)

    async with connect() as conn:
        days = await mq.editions(conn, user_id)
        assert [row.day for row in days] == [tuesday, monday], "editions are not newest first"
        assert len(await mq.edition(conn, user_id, monday)) == 1
        assert len(await mq.edition(conn, user_id, tuesday)) == len(ids) - 1


async def test_an_edition_records_the_profile_version_that_produced_it(seeded):
    """An edition is a one-time publication; a later profile does not rewrite an earlier day."""
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    monday, tuesday = date(2026, 9, 14), date(2026, 9, 15)
    await _rescore_on(user_id, ids[:1], monday, version=1)
    await _rescore_on(user_id, ids[1:], tuesday, version=2)

    async with connect() as conn:
        by_day = {row.day: row for row in await mq.editions(conn, user_id)}
    assert by_day[monday].profile_version == 1
    assert by_day[tuesday].profile_version == 2


async def test_a_posting_never_scored_belongs_to_no_edition(seeded):
    """Retrieval is cumulative and rerank_limit caps scoring; unscored is not a recommendation."""
    user_id = seeded["user_id"]
    async with connect() as conn:
        await conn.execute(
            sa.text(
                "UPDATE user_job_match SET llm_score = NULL, scored_at = NULL "
                "WHERE user_id = :user"
            ),
            {"user": user_id},
        )
        assert await mq.editions(conn, user_id) == []


async def test_a_day_with_no_scoring_simply_has_no_edition(seeded):
    """A runner that missed a day leaves a gap in the list, not a hole in anyone's history."""
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    monday, wednesday = date(2026, 9, 14), date(2026, 9, 16)
    await _rescore_on(user_id, ids[:1], monday)
    await _rescore_on(user_id, ids[1:], wednesday)

    async with connect() as conn:
        days = [row.day for row in await mq.editions(conn, user_id)]
        assert days == [wednesday, monday]
        assert await mq.edition(conn, user_id, monday + timedelta(days=1)) == []


async def test_an_edition_survives_its_postings_ageing_past_the_horizon(seeded):
    """Browsing back to an old edition must still show what it recommended.

    The horizon bounds what may be *retrieved*; it must not reach into what was already
    published, or a user's own history would rot from underneath them.
    """
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    long_ago = date(2026, 1, 5)
    await _rescore_on(user_id, ids, long_ago)
    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE job SET posted_at = now() - interval '300 days', "
            "first_seen_at = now() - interval '300 days'"
        )
        rows = await mq.edition(conn, user_id, long_ago)
    assert len(rows) == len(ids)
