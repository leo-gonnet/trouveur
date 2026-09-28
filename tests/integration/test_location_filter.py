"""The location filter, run against real facet rows.

It is the only hard filter, so every case it gets wrong is a posting the reader never learns
existed. Recall first: a posting passes whenever its location cannot rule it out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.integration.seed import seed_corpus, seed_user
from trouveur.db.engine import connect
from trouveur.db.queries import match as mq
from trouveur.ingest import places

pytestmark = pytest.mark.asyncio

SINCE = datetime.now(UTC) - timedelta(days=7)
VIENNA = places.resolve("Wien", "AT").id
MODLING = places.resolve("Mödling", "AT").id
GRAZ = places.resolve("Graz", "AT").id
BERLIN = places.resolve("Berlin", "DE").id
GENEVA = places.resolve("Genève", "CH").id

# name: (countries, place_ids, unplaced_countries, work_mode)
POSTINGS = {
    "no location": ([], [], [], "unknown"),
    "Mödling, near Vienna": (["AT"], [MODLING], [], "onsite"),
    "Graz, 150 km from Vienna": (["AT"], [GRAZ], [], "onsite"),
    "Austria, town unknown": (["AT"], [], ["AT"], "unknown"),
    "Germany, town unknown": (["DE"], [], ["DE"], "unknown"),
    "Berlin": (["DE"], [BERLIN], [], "onsite"),
    "fully remote, posted from Germany": (["DE"], [], ["DE"], "remote"),
    "fully remote, no location": ([], [], [], "remote"),
    "Geneva, Switzerland or the US": ([], [], ["CH", "US"], "onsite"),
}


async def _passes(profile, posting: str) -> bool:
    countries, place_ids, unplaced, work_mode = POSTINGS[posting]
    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE job SET posted_at = now(), first_seen_at = now(), closed_at = NULL"
        )
        await conn.exec_driver_sql(
            "UPDATE job_facet SET countries = $1, place_ids = $2, unplaced_countries = $3, "
            "work_mode = $4",
            (countries, place_ids, unplaced, work_mode),
        )
        total = (await conn.exec_driver_sql("SELECT count(*) FROM job")).scalar_one()
        kept = await mq.count_pending_rerank(conn, profile, SINCE)
    assert kept in (0, total)
    return kept == total


@pytest.mark.parametrize(
    ("posting", "kept"),
    [
        ("no location", True),
        ("Mödling, near Vienna", True),
        ("Graz, 150 km from Vienna", False),
        # Might be in Vienna: dropping it would lose recall for a derivation gap.
        ("Austria, town unknown", True),
        ("Germany, town unknown", False),
        ("Berlin", False),
        ("fully remote, posted from Germany", True),
        ("fully remote, no location", True),
        ("Geneva, Switzerland or the US", False),
    ],
)
async def test_a_city_filter_keeps_what_its_location_cannot_rule_out(
    clean_db, gh_board, aa_listing, aa_detail, posting, kept
):
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, profile = await seed_user(countries=[], city_ids=[VIENNA], radius_km=30)
    assert await _passes(profile, posting) is kept


async def test_countries_and_cities_add_up_rather_than_narrow(
    clean_db, gh_board, aa_listing, aa_detail
):
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, profile = await seed_user(countries=["DE"], city_ids=[VIENNA], radius_km=30)
    assert await _passes(profile, "Berlin")
    assert await _passes(profile, "Mödling, near Vienna")
    assert not await _passes(profile, "Graz, 150 km from Vienna")


async def test_nothing_picked_keeps_everywhere(clean_db, gh_board, aa_listing, aa_detail):
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, profile = await seed_user(countries=[], city_ids=[], remote_anywhere=False)
    assert await _passes(profile, "Graz, 150 km from Vienna")


async def test_a_remote_posting_with_no_country_needs_remote_anywhere(
    clean_db, gh_board, aa_listing, aa_detail
):
    """A reader who declined fully remote roles was recommended one: it named no country, and a
    posting with no country passed whatever the reader had said about remote work."""
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, declined = await seed_user(countries=[], city_ids=[VIENNA], remote_anywhere=False)
    assert not await _passes(declined, "fully remote, no location")
    assert await _passes(declined, "no location")


async def test_a_town_that_could_be_several_places_reaches_a_reader_in_any_of_them(
    clean_db, gh_board, aa_listing, aa_detail
):
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, in_us = await seed_user(countries=["US"], city_ids=[])
    _, near_geneva = await seed_user(
        "geneva@example.test", countries=[], city_ids=[GENEVA], radius_km=30
    )
    assert await _passes(in_us, "Geneva, Switzerland or the US")
    assert await _passes(near_geneva, "Geneva, Switzerland or the US")


async def _kept_beside_a_berlin_twin(profile, copy_says: str, *, twin_open: bool = True) -> int:
    """A copy with no location facets and its twin in Berlin, both open and in one group."""
    async with connect() as conn:
        rows = await conn.exec_driver_sql("SELECT id FROM job ORDER BY id LIMIT 2")
        copy, twin = [row[0] for row in rows.all()]
        await conn.exec_driver_sql(
            "UPDATE job SET posted_at = now(), first_seen_at = now(), closed_at = now(), "
            "dedup_group = NULL"
        )
        await conn.exec_driver_sql(
            "UPDATE job SET closed_at = NULL, dedup_group = $1 WHERE id = ANY($2)",
            (b"\x01", [copy, twin]),
        )
        if not twin_open:
            await conn.exec_driver_sql("UPDATE job SET closed_at = now() WHERE id = $1", (twin,))
        await conn.exec_driver_sql(
            "UPDATE job SET location_text = $1 WHERE id = $2", (copy_says, copy)
        )
        await conn.exec_driver_sql(
            "UPDATE job_facet SET countries = $1, place_ids = $2, unplaced_countries = '{}', "
            "work_mode = 'onsite' WHERE job_id = $3",
            ([], [], copy),
        )
        await conn.exec_driver_sql(
            "UPDATE job_facet SET countries = $1, place_ids = $2, unplaced_countries = '{}', "
            "work_mode = 'onsite' WHERE job_id = $3",
            (["DE"], [BERLIN], twin),
        )
        return await mq.count_pending_rerank(conn, profile, SINCE)


async def test_a_copy_whose_source_named_no_place_is_judged_by_its_twins(
    clean_db, gh_board, aa_listing, aa_detail
):
    """Arbeitnow blanks the location of the boards it copies. Passed as unstated, Elastic's
    London role reached a reader in Vienna while its own board said London."""
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, vienna = await seed_user(countries=[], city_ids=[VIENNA], radius_km=30)
    _, germany = await seed_user("germany@example.test", countries=["DE"], city_ids=[])
    assert await _kept_beside_a_berlin_twin(vienna, "") == 0
    assert await _kept_beside_a_berlin_twin(germany, "") == 2


async def test_a_copy_that_names_its_own_town_is_never_judged_by_a_twin(
    clean_db, gh_board, aa_listing, aa_detail
):
    """The same role in Sobernheim and in Coburg is two places. A town we cannot resolve is
    still the posting's own, and the reader who lives there must keep it."""
    await seed_corpus(gh_board, aa_listing, aa_detail)
    _, vienna = await seed_user(countries=[], city_ids=[VIENNA], radius_km=30)
    assert await _kept_beside_a_berlin_twin(vienna, "Sobernheim") == 1
    assert await _kept_beside_a_berlin_twin(vienna, "", twin_open=False) == 1
