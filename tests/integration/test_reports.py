"""A reader's "I found this job somewhere else", answered end to end.

One test per answer, because the answer is the whole product of this feature: a wrong one sends
somebody to look at the wrong part of the pipeline, or tells a reader we never collected a job we
collected. They run through the real stage the runner calls, over a corpus seeded through the
real ingest path, so the lookup has to cope with links spelled the way a reader pastes them.
"""

from __future__ import annotations

import sqlalchemy as sa

from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import match as match_q
from trouveur.db.queries import reports as reports_q
from trouveur.db.schema import discovery_lead, job, source_tenant, user_report
from trouveur.discovery import reports
from trouveur.models import ReportOutcome, ReportReason


async def _file(user_id: int, url: str) -> None:
    async with connect() as conn:
        await reports_q.create(conn, user_id=user_id, url=url)


async def _answer(user_id: int, url: str):
    """File a link, let the runner's stage answer it, and read the answer back."""
    await _file(user_id, url)
    await reports.answer_pending()
    async with connect() as conn:
        return (
            await conn.execute(
                sa.select(user_report).where(
                    user_report.c.user_id == user_id, user_report.c.url == url
                )
            )
        ).one()


async def _url_of(job_id: int) -> str:
    async with connect() as conn:
        return (await conn.execute(sa.select(job.c.url).where(job.c.id == job_id))).scalar()


async def _other_job(exclude: int) -> int:
    """A greenhouse posting the seeded reader has NOT been shown."""
    async with connect() as conn:
        return (
            await conn.execute(
                sa.select(job.c.id)
                .where(job.c.source == "greenhouse", job.c.id != exclude)
                .limit(1)
            )
        ).scalar()


async def test_a_job_we_showed_is_answered_as_shown(seeded):
    row = await _answer(seeded["user_id"], await _url_of(seeded["job_id"]))
    assert row.outcome == ReportOutcome.HAD_AND_RECOMMENDED
    assert row.reason is None
    assert row.job_id == seeded["job_id"], "the answer has to point at our own copy"
    assert row.answered_at is not None


async def test_a_job_we_hold_and_never_retrieved_says_so(seeded):
    """The recall bug worth hearing about: it was in the corpus, in the area, and our own
    retrieval never put it in front of the reader."""
    other = await _other_job(seeded["job_id"])
    row = await _answer(seeded["user_id"], await _url_of(other))
    assert (row.outcome, row.reason) == (
        ReportOutcome.HAD_NOT_RECOMMENDED, ReportReason.NOT_RETRIEVED
    )
    assert row.job_id == other


async def test_a_job_retrieved_but_never_scored_says_so(seeded):
    """Costs nothing to fix -- credit, or the switch on Settings -- and is invisible from the
    reader's side, who sees only that the job never arrived."""
    other = await _other_job(seeded["job_id"])
    async with connect() as conn:
        await match_q.upsert_matches(
            conn,
            [{
                "user_id": seeded["user_id"], "job_id": other,
                "profile_version": seeded["profile"].version, "retrieval_score": 0.4,
                "dense_rank": 2, "lexical_rank": None,
            }],
        )
    row = await _answer(seeded["user_id"], await _url_of(other))
    assert (row.outcome, row.reason) == (
        ReportOutcome.HAD_NOT_RECOMMENDED, ReportReason.NOT_SCORED
    )


async def test_a_job_outside_the_readers_area_names_the_filter(seeded):
    """Read with another reader's area, because the filter is per reader: the same posting is
    inside one area and outside another, which is why the answer cannot live on the lead."""
    from tests.integration.seed import seed_user

    far_away, _ = await seed_user(email="paris@example.test", countries=["FR"])
    row = await _answer(far_away, await _url_of(await _other_job(seeded["job_id"])))
    assert (row.outcome, row.reason) == (
        ReportOutcome.HAD_NOT_RECOMMENDED, ReportReason.LOCATION
    )


async def test_a_link_spelled_differently_still_finds_our_copy(seeded):
    """Nobody pastes the URL we stored. This is the same posting on the board's other host, with
    the tracking parameter the site they found it on added -- and it has to match, or we tell a
    reader we never collected a posting sitting in the corpus."""
    other = await _other_job(seeded["job_id"])
    external_id = None
    async with connect() as conn:
        external_id = (
            await conn.execute(sa.select(job.c.external_id).where(job.c.id == other))
        ).scalar()
    _, _, job_number = external_id.partition(":")
    row = await _answer(
        seeded["user_id"],
        f"https://boards.greenhouse.io/beispiel/jobs/{job_number}/?utm_source=linkedin",
    )
    assert row.job_id == other, "the posting's own id inside the board it belongs to"


async def test_a_link_we_cannot_read_still_finds_our_copy_by_url(seeded):
    """An aggregator states the employer's own advert, so what we stored for an Arbeitsagentur
    posting is a company careers URL no rule reads. Resolving it is not how it is found: the URL
    we stored is, which is the whole reason the lookup falls back to it."""
    async with connect() as conn:
        url = (
            await conn.execute(
                sa.select(job.c.url).where(job.c.source == "arbeitsagentur")
            )
        ).scalar()
    assert url.startswith("https://beispiel.example"), "fixture no longer states an employer URL"
    row = await _answer(seeded["user_id"], url)
    assert row.outcome == ReportOutcome.HAD_NOT_RECOMMENDED
    assert row.job_id is not None
    assert row.source is None, "the link named no platform; the posting was found anyway"


async def test_a_board_we_sweep_without_the_job_is_a_gap_in_our_own_sweep(seeded):
    """Arbeitsagentur is swept whole, so a posting on it that we do not hold is not a board we
    are missing -- it is a posting our own sweep lost."""
    row = await _answer(
        seeded["user_id"],
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-9999999999-S",
    )
    assert row.outcome == ReportOutcome.MISSING_JOB
    assert (row.source, row.scope) == ("arbeitsagentur", None)


async def test_a_board_new_to_us_is_proposed_and_the_reader_is_told(seeded):
    """The lead and the candidate are written whatever the answer is, so one reported link can
    bring that company's whole board with it."""
    row = await _answer(
        seeded["user_id"], "https://job-boards.greenhouse.io/neuefirma/jobs/9001"
    )
    assert row.outcome == ReportOutcome.MISSING_BOARD
    assert (row.source, row.scope) == ("greenhouse", "neuefirma")
    async with connect() as conn:
        candidate = (
            await conn.execute(
                sa.select(source_tenant).where(source_tenant.c.scope == "neuefirma")
            )
        ).one()
        lead = (
            await conn.execute(
                sa.select(discovery_lead).where(discovery_lead.c.origin == "user_report")
            )
        ).one()
    assert (candidate.source, candidate.enabled, candidate.origin) == (
        "greenhouse", False, "discovered"
    ), "a reader's report must not enlarge the crawl on its own"
    assert lead.scope == "neuefirma"


async def test_an_enabled_board_that_lacks_the_job_is_a_gap_too(seeded):
    """The same answer for a board source, once the board is actually being swept. A candidate
    does not count: it is a proposal, and nothing has asked that board for anything yet."""
    async with connect() as conn:
        await admin_q.add_tenants(conn, source="greenhouse", scopes=["beispiel"])
        await admin_q.set_tenant_enabled(
            conn, source="greenhouse", scope="beispiel", enabled=True
        )
    row = await _answer(
        seeded["user_id"], "https://job-boards.greenhouse.io/beispiel/jobs/9002"
    )
    assert row.outcome == ReportOutcome.MISSING_JOB


async def test_a_platform_no_rule_reads_is_an_unknown_platform(seeded):
    """Still worth filing: the lead keeps the raw URL, so the day a rule for that platform lands
    a RESOLVE_VERSION bump reads every one of these without asking anybody for anything."""
    row = await _answer(
        seeded["user_id"], "https://jobs.smartrecruiters.com/AcmeGmbH/744000139104759"
    )
    assert row.outcome == ReportOutcome.UNKNOWN_PLATFORM
    assert (row.source, row.scope, row.job_id) == (None, None, None)
    async with connect() as conn:
        lead = (
            await conn.execute(
                sa.select(discovery_lead).where(discovery_lead.c.origin == "user_report")
            )
        ).one()
    assert lead.host == "jobs.smartrecruiters.com", "the host is the case for writing a rule"


async def test_an_answer_is_never_rewritten(seeded):
    """What a reader was told has to keep saying what it said, exactly as an edition does. The
    corpus moves under it: the board gets promoted, the posting arrives in next week's sweep."""
    url = "https://job-boards.greenhouse.io/neuefirma/jobs/9003"
    first = await _answer(seeded["user_id"], url)
    async with connect() as conn:
        await admin_q.set_tenant_enabled(
            conn, source="greenhouse", scope="neuefirma", enabled=True
        )
    assert await reports.answer_pending() == 0, "an answered report is not in the queue"
    async with connect() as conn:
        again = (
            await conn.execute(sa.select(user_report).where(user_report.c.id == first.id))
        ).one()
    assert (again.outcome, again.answered_at) == (first.outcome, first.answered_at)


async def test_the_same_link_twice_while_waiting_is_one_question(seeded):
    """A double-pressed button is one question. Once answered the same link can be asked again,
    because the answer may have changed."""
    url = "https://job-boards.greenhouse.io/neuefirma/jobs/9004"
    await _file(seeded["user_id"], url)
    await _file(seeded["user_id"], url)
    async with connect() as conn:
        waiting = (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(user_report)
                .where(user_report.c.url == url)
            )
        ).scalar()
    assert waiting == 1
    await reports.answer_pending()
    await _file(seeded["user_id"], url)
    async with connect() as conn:
        total = (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(user_report)
                .where(user_report.c.url == url)
            )
        ).scalar()
    assert total == 2, "asking again after an answer files a second question"


async def test_the_coverage_report_counts_what_readers_were_told(seeded):
    from trouveur.coverage import report

    await _answer(seeded["user_id"], await _url_of(seeded["job_id"]))
    await _file(seeded["user_id"], "https://jobs.smartrecruiters.com/AcmeGmbH/1")
    async with connect() as conn:
        card = await report(conn, horizon_days=36500)
    assert card.reports is not None, "a section that stays None is a section nobody reads"
    assert card.reports.reports == 2
    assert card.reports.recommended == 1
    assert card.reports.waiting == 1
    assert card.as_dict()["reports"]["recommended"] == 1
