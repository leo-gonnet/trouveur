"""Recommendations published one day at a time, and never rewritten.

The page used to be one list that only grew, ordered by score, so a strong posting from three
weeks ago outranked everything that arrived this morning and stayed there until it was
dismissed. An edition gives the page a time axis without a score cut: the day is the cut.

An edition used to be DERIVED, by grouping `user_job_match` on `scored_at::date`. A row carries
one scored_at, so re-scoring a posting moved it out of the day it was published in -- and
`reset_scores` nulled the column outright on a profile change, erasing every edition the user
had while the page promised the opposite. These tests are what stops that coming back.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from trouveur.db.engine import connect
from trouveur.db.queries import match as mq
from trouveur.db.queries import users as users_q

pytestmark = pytest.mark.asyncio


async def _publish(user_id: int, job_ids: list[int], day: date, version: int = 1) -> None:
    """Publish an edition the way a match run would."""
    async with connect() as conn:
        await mq.publish_edition(
            conn,
            [
                {
                    "user_id": user_id, "day": day, "job_id": job_id,
                    "profile_version": version, "llm_score": 80,
                    "llm_reason": "probe", "llm_red_flags": [],
                }
                for job_id in job_ids
            ],
        )


async def _all_match_ids(user_id: int) -> list[int]:
    """Every posting in the seeded corpus, matched to this user.

    The `seeded` fixture publishes exactly one, which is enough to render a card and not enough
    to split across two days -- so the rest are matched here, and none of them is published.
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
        await conn.execute(
            sa.text("DELETE FROM user_edition_item WHERE user_id = :user"), {"user": user_id}
        )
    return job_ids


async def test_postings_published_on_different_days_land_in_different_editions(seeded):
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    assert len(ids) >= 2, "need at least two matches to split across two days"

    monday, tuesday = date(2026, 9, 14), date(2026, 9, 15)
    await _publish(user_id, ids[:1], monday)
    await _publish(user_id, ids[1:], tuesday)

    async with connect() as conn:
        days = await mq.editions(conn, user_id)
        assert [row.day for row in days] == [tuesday, monday], "editions are not newest first"
        assert len(await mq.edition(conn, user_id, monday, 1, limit=100)) == 1
        assert len(await mq.edition(conn, user_id, tuesday, 1, limit=100)) == len(ids) - 1


async def test_an_edition_records_the_profile_version_that_produced_it(seeded):
    """An edition is a one-time publication; a later profile does not rewrite an earlier day."""
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    monday, tuesday = date(2026, 9, 14), date(2026, 9, 15)
    await _publish(user_id, ids[:1], monday, version=1)
    await _publish(user_id, ids[1:], tuesday, version=2)

    async with connect() as conn:
        by_day = {row.day: row for row in await mq.editions(conn, user_id)}
    assert by_day[monday].profile_version == 1
    assert by_day[tuesday].profile_version == 2


async def test_changing_the_profile_leaves_every_published_edition_standing(seeded):
    """The regression that made this file necessary.

    `reset_scores` nulled llm_score and scored_at for every row whenever a scoring field
    changed, and the edition read required both to be set -- so editing a profile emptied the
    Recommendations page of every day the user had ever been sent.
    """
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    monday = date(2026, 9, 14)
    await _publish(user_id, ids, monday)

    async with connect() as conn:
        version, rescore = await users_q.save_profile(
            conn, user_id, {"title": "Something Else Entirely"}
        )
        assert rescore, "changing the title must count as a scoring change"
        assert version > 1
        rows = await mq.edition(conn, user_id, monday, 1, limit=100)
        assert len(rows) == len(ids), "a profile change erased a published edition"


async def test_re_scoring_a_posting_does_not_move_it_out_of_its_edition(seeded):
    """The other half: a posting scored again lands in the new day and stays in the old one."""
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    monday, friday = date(2026, 9, 14), date(2026, 9, 18)
    await _publish(user_id, ids[:1], monday, version=1)
    await _publish(user_id, ids[:1], friday, version=2)

    async with connect() as conn:
        monday_rows = await mq.edition(conn, user_id, monday, 1, limit=100)
        assert len(monday_rows) == 1, "monday lost its posting"
        assert len(await mq.edition(conn, user_id, friday, 2, limit=100)) == 1


async def test_a_posting_cannot_be_recommended_twice_under_one_profile_version(seeded):
    """Only a profile change may bring a posting back; a repeat reads as the system stuttering.

    pending_rerank already declines to re-score it, but that is a WHERE clause someone can edit.
    """
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    await _publish(user_id, ids[:1], date(2026, 9, 14), version=1)

    with pytest.raises(Exception, match="uq_edition_item_once_per_version"):
        await _publish(user_id, ids[:1], date(2026, 9, 15), version=1)


async def test_a_posting_never_published_belongs_to_no_edition(seeded):
    """Retrieval is cumulative and can outrun scoring; unscored is not a recommendation."""
    user_id = seeded["user_id"]
    await _all_match_ids(user_id)
    async with connect() as conn:
        assert await mq.editions(conn, user_id) == []


async def test_a_day_with_no_scoring_simply_has_no_edition(seeded):
    """A runner that missed a day leaves a gap in the list, not a hole in anyone's history."""
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    monday, wednesday = date(2026, 9, 14), date(2026, 9, 16)
    await _publish(user_id, ids[:1], monday)
    await _publish(user_id, ids[1:], wednesday)

    async with connect() as conn:
        days = [row.day for row in await mq.editions(conn, user_id)]
        assert days == [wednesday, monday]
        assert await mq.edition(conn, user_id, monday + timedelta(days=1), 1, limit=100) == []


async def test_an_edition_survives_its_postings_ageing_past_the_horizon(seeded):
    """Browsing back to an old edition must still show what it recommended.

    The horizon bounds what may be *retrieved*; it must not reach into what was already
    published, or a user's own history would rot from underneath them.
    """
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    long_ago = date(2026, 1, 5)
    await _publish(user_id, ids, long_ago)
    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE job SET posted_at = now() - interval '300 days', "
            "first_seen_at = now() - interval '300 days'"
        )
        rows = await mq.edition(conn, user_id, long_ago, 1, limit=100)
    assert len(rows) == len(ids)


async def test_a_posting_that_closes_stays_in_the_edition_it_was_published_in(seeded):
    """Otherwise a published day shrinks as the world moves on, and its count stops matching.

    The card marks it closed; the row does not vanish.
    """
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    monday = date(2026, 9, 14)
    await _publish(user_id, ids, monday)

    async with connect() as conn:
        await conn.exec_driver_sql("UPDATE job SET closed_at = now()")
        rows = await mq.edition(conn, user_id, monday, 1, limit=100)
        counted = {row.day: row.postings for row in await mq.editions(conn, user_id)}
    assert len(rows) == len(ids), "closing a posting emptied a published edition"
    assert counted[monday] == len(rows), "the dropdown count and the list disagree"


async def test_a_profile_change_adds_an_edition_beside_the_day_it_changed_on(seeded):
    """Nothing published is ever destroyed, including today's.

    A profile change used to REPLACE the day's edition, which is why saving one needed a confirm
    page: the save destroyed something. It was also the single exception to "an edition is never
    rewritten", and an exception in an invariant is what eventually gets taken for the rule.
    """
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    today = date(2026, 9, 24)
    await _publish(user_id, ids[:1], today, version=1)
    await _publish(user_id, ids[1:], today, version=2)

    async with connect() as conn:
        listed = [(row.day, row.profile_version, row.postings)
                  for row in await mq.editions(conn, user_id)]
        first = await mq.edition(conn, user_id, today, 1, limit=100)
        second = await mq.edition(conn, user_id, today, 2, limit=100)

    assert listed == [(today, 2, len(ids) - 1), (today, 1, 1)], "newest version first"
    assert len(first) == 1, "the edition read under the old profile was destroyed"
    assert len(second) == len(ids) - 1


async def test_one_posting_may_sit_in_two_editions_under_two_profiles(seeded):
    """The once-per-version constraint is per VERSION, which is what makes versioning work.

    A reader who changes their profile is asking to be shown the corpus again under new terms, so
    the same posting appearing in both editions is the point. The constraint still refuses it
    twice under ONE version, which is the case that reads as the system repeating itself.
    """
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    today = date(2026, 9, 24)
    await _publish(user_id, ids[:1], today, version=1)
    await _publish(user_id, ids[:1], today, version=2)

    async with connect() as conn:
        assert len(await mq.edition(conn, user_id, today, 1, limit=100)) == 1
        assert len(await mq.edition(conn, user_id, today, 2, limit=100)) == 1
