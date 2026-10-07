"""The coverage report, against a real corpus.

Every number here is one an operator decides with: whether a new source helped, and which source
would take postings with it if it went away. A wrong number reads exactly like a right one, so
each is checked against rows the test placed itself.
"""

from __future__ import annotations

import re

import pytest

from tests.integration.seed import seed_corpus, seed_user
from tests.integration.test_web_pages import admin  # noqa: F401 -- a logged-in admin client
from trouveur import coverage
from trouveur.config import get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import users as users_q
from trouveur.ingest import places
from trouveur.models import TenantOrigin

pytestmark = pytest.mark.asyncio

BERLIN = places.resolve("Berlin", "DE").id
VIENNA = places.resolve("Wien", "AT").id


async def _place_every_posting_in(country: str, place_id: int) -> None:
    """Give the whole seeded corpus one location, so the area under test is the only variable."""
    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE job SET posted_at = now(), first_seen_at = now(), closed_at = NULL"
        )
        await conn.exec_driver_sql(
            "UPDATE job_facet SET countries = $1, place_ids = $2, unplaced_countries = $3, "
            "work_mode = 'onsite'",
            ([country], [place_id], [country]),
        )


async def _report() -> coverage.Report:
    async with connect() as conn:
        return await coverage.report(conn, get_settings().retrieval_horizon_days)


async def test_the_area_is_read_from_the_profile_and_filters_the_corpus(
    clean_db, gh_board, aa_listing, aa_detail
):
    """A reader looking somewhere else must see zero, not the whole corpus.

    The filter is the entire point of the report: without it every area shows the same totals and
    a new source looks as though it helped everybody equally.
    """
    await seed_corpus(gh_board, aa_listing, aa_detail)
    await _place_every_posting_in("DE", BERLIN)
    await seed_user("berlin@example.test", countries=[], city_ids=[BERLIN], radius_km=30)
    await seed_user("vienna@example.test", countries=[], city_ids=[VIENNA], radius_km=30)

    areas = (await _report()).areas
    assert len(areas) == 2, "two different areas collapsed into one"
    berlin = next(entry for entry in areas if places.get(BERLIN).name in entry.area.label)
    vienna = next(entry for entry in areas if places.get(VIENNA).name in entry.area.label)
    assert berlin.open_jobs > 0, "the corpus was placed in Berlin but Berlin's area is empty"
    assert vienna.open_jobs == 0, (
        "a reader in Vienna was credited with postings that are all in Berlin"
    )


async def test_two_readers_who_picked_the_same_area_are_one_row(
    clean_db, gh_board, aa_listing, aa_detail
):
    """One corpus counted twice reads as twice the coverage, and the page would then carry who
    reads what, which it has no use for. The arrays are stored as the form wrote them, so the
    same area arrives in two spellings."""
    await seed_corpus(gh_board, aa_listing, aa_detail)
    await seed_user("one@example.test", countries=["DE", "AT"], city_ids=[])
    await seed_user("two@example.test", countries=["AT", "DE"], city_ids=[])

    areas = (await _report()).areas
    assert len(areas) == 1, f"one area was reported as {len(areas)}"
    assert areas[0].area.readers == 2


async def test_an_inactive_reader_is_not_an_area(clean_db, gh_board, aa_listing, aa_detail):
    """A disabled account cannot be shown anything, so its area is not one to collect for."""
    await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, _ = await seed_user("gone@example.test", countries=["DE"])
    assert (await _report()).areas, "the fixture is wrong: the reader had no area to begin with"
    async with connect() as conn:
        await users_q.set_active(conn, user_id, is_active=False)
    assert (await _report()).areas == []


async def test_a_posting_a_second_source_also_has_is_not_counted_as_irreplaceable(
    clean_db, gh_board, aa_listing, aa_detail
):
    """`only_source` is what dropping a source would cost, so a copy elsewhere disqualifies the
    posting. Marked by dedup_group, the only cross-source identity there is."""
    await seed_corpus(gh_board, aa_listing, aa_detail)
    await _place_every_posting_in("DE", BERLIN)
    await seed_user("berlin@example.test", countries=["DE"])

    before = {row.source: row for row in (await _report()).areas[0].sources}
    assert len(before) > 1, "the fixture corpus has only one source; nothing to share"

    first, second = sorted(before)[:2]
    async with connect() as conn:
        ids = [
            (
                await conn.exec_driver_sql(
                    "SELECT id FROM job WHERE source = $1 ORDER BY id LIMIT 1", (name,)
                )
            ).scalar_one()
            for name in (first, second)
        ]
        await conn.exec_driver_sql(
            "UPDATE job SET dedup_group = $1, dedup_version = 1 WHERE id = ANY($2)",
            (b"shared-role", ids),
        )

    after = {row.source: row for row in (await _report()).areas[0].sources}
    for name in (first, second):
        assert after[name].only_source == before[name].only_source - 1, (
            f"{name} still counts a posting another source also has as irreplaceable"
        )
        assert after[name].open_jobs == before[name].open_jobs, "a marker changed a count"


async def test_a_posting_with_no_vector_is_open_but_not_retrievable(
    clean_db, gh_board, aa_listing, aa_detail
):
    """The gap between the two is a posting collected but invisible to recommendations. Reported
    as one number it reads as poor recall instead."""
    await seed_corpus(gh_board, aa_listing, aa_detail)
    await _place_every_posting_in("DE", BERLIN)
    await seed_user("berlin@example.test", countries=["DE"])

    before = (await _report()).areas[0]
    assert before.open_jobs > 0
    assert before.retrievable_jobs == before.open_jobs, "the seeded corpus is not fully embedded"

    async with connect() as conn:
        await conn.exec_driver_sql("DELETE FROM job_embedding")
    after = (await _report()).areas[0]
    assert after.open_jobs == before.open_jobs
    assert after.retrievable_jobs == 0


async def test_a_dropped_board_is_counted_as_dropped_and_stops_asking_for_a_decision(
    clean_db, gh_board, aa_listing, aa_detail
):
    """Dropping keeps the row so discovery cannot propose the board again -- which would also
    leave it on the list of boards awaiting a decision for ever."""
    await seed_corpus(gh_board, aa_listing, aa_detail)
    await seed_user("berlin@example.test", countries=["DE"])

    async with connect() as conn:
        await admin_q.add_tenants(
            conn, "greenhouse", ["tried-this-one"], enabled=False,
            origin=TenantOrigin.DISCOVERED,
        )
        rows, totals = await admin_q.tenants_needing_attention(conn)
        assert any(row.scope == "tried-this-one" for row in rows), "a candidate was not listed"
        assert totals["candidates"] == 1

        assert await admin_q.drop_tenant(conn, "greenhouse", "tried-this-one", note="empty board")
        rows, totals = await admin_q.tenants_needing_attention(conn)

    assert not any(row.scope == "tried-this-one" for row in rows), (
        "a board already decided against is still asking for a decision"
    )
    assert totals == {"tenants": 1, "sweeping": 0, "failing": 0, "candidates": 0, "dropped": 1}

    states = {row.source: row for row in (await _report()).tenants}
    assert states["greenhouse"].dropped == 1
    assert states["greenhouse"].candidates == 0
    # The four states partition the boards: a column that can double-count makes a table of
    # states one a reader cannot check against the total.
    row = states["greenhouse"]
    assert row.sweeping + row.candidates + row.disabled + row.dropped == 1

    async with connect() as conn:
        await admin_q.set_tenant_enabled(conn, "greenhouse", "tried-this-one", True)
    reopened = {row.source: row for row in (await _report()).tenants}
    assert reopened["greenhouse"].dropped == 0, "enabling a board left it marked as rejected"
    assert reopened["greenhouse"].sweeping == 1


def _rendered_row(body: str, source: str) -> list[int]:
    """The three numbers the coverage table shows for one source."""
    cells = re.search(rf">\s*{re.escape(source)}\s*</td>(.*?)</tr>", body, re.S)
    assert cells, f"{source} has no row on the page"
    return [
        int(value.replace(",", "")) for value in re.findall(r">\s*([\d,]+)\s*<", cells[1])
    ]


async def test_the_page_shows_the_same_numbers_as_the_command(admin):  # noqa: F811
    """One query serves both, so the only way they can disagree is the template reading the wrong
    column -- which is why this renders a real row rather than checking an empty page returned."""
    await _place_every_posting_in("DE", BERLIN)
    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE user_profile SET countries = $1, city_ids = '{}'", (["DE"],)
        )
        by_source: dict[str, list[int]] = {}
        for row in await conn.exec_driver_sql("SELECT id, source FROM job ORDER BY id"):
            by_source.setdefault(row.source, []).append(row.id)
        biggest = max(by_source, key=lambda name: len(by_source[name]))
        other = next(name for name in by_source if name != biggest)
        # Two postings lose their vector and one more is also held by another source, so that
        # row's three numbers are three different numbers. Left equal -- which they are in a
        # freshly seeded corpus -- a template reading the wrong column shows the right answer by
        # accident and this test proves nothing.
        await conn.exec_driver_sql(
            "DELETE FROM job_embedding WHERE job_id = ANY($1)", (by_source[biggest][:2],)
        )
        await conn.exec_driver_sql(
            "UPDATE job SET dedup_group = $1, dedup_version = 1 WHERE id = ANY($2)",
            (b"shared-role", [by_source[biggest][-1], by_source[other][0]]),
        )

    card = await _report()
    assert len(card.areas) == 1, "this test reads the first table; it expects one area"
    assert any(
        len({row.open_jobs, row.retrievable_jobs, row.only_source}) == 3
        for row in card.areas[0].sources
    ), "no row holds three different numbers, so a wrong column would still read correctly"

    response = await admin.get("/admin/coverage")
    assert response.status_code == 200
    # Both tables name a source in the first cell, so only the coverage one is read.
    body = response.text.split("Crawl set by source")[0]
    assert card.areas[0].area.label in body
    for row in card.areas[0].sources:
        assert _rendered_row(body, row.source)[:3] == [
            row.open_jobs, row.retrievable_jobs, row.only_source
        ], f"{row.source}: the page and the command disagree"


async def test_an_area_we_collected_nothing_for_says_so(admin):  # noqa: F811
    """The whole point of the report is finding the area nobody is collecting for, and an area
    with no rows renders as a table with no body unless it is called out."""
    await _place_every_posting_in("DE", BERLIN)
    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE user_profile SET countries = '{}', city_ids = $1, remote_anywhere = false",
            ([VIENNA],),
        )
    assert (await _report()).areas[0].open_jobs == 0, "the fixture left postings in the area"

    body = (await admin.get("/admin/coverage")).text
    assert "Nothing at all has been collected for this area" in body
