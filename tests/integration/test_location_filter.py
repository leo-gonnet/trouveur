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

# name: (countries, place_ids, unplaced_countries, work_mode)
POSTINGS = {
    "no location": ([], [], [], "unknown"),
    "Mödling, near Vienna": (["AT"], [MODLING], [], "onsite"),
    "Graz, 150 km from Vienna": (["AT"], [GRAZ], [], "onsite"),
    "Austria, town unknown": (["AT"], [], ["AT"], "unknown"),
    "Germany, town unknown": (["DE"], [], ["DE"], "unknown"),
    "Berlin": (["DE"], [BERLIN], [], "onsite"),
    "fully remote, posted from Germany": (["DE"], [], ["DE"], "remote"),
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
