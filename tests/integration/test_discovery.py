"""Discovery against a real Postgres: mine the archive, resolve, propose a board.

What only a database can show here is that the two version columns really are queues -- a stage
re-reads exactly the rows below its version and nothing else -- and that promoting a candidate
cannot overwrite a decision an operator already made.
"""

from __future__ import annotations

from trouveur import versions
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.discovery import work as discovery
from trouveur.models import DocumentKind, RawDocument, TenantOrigin

# An employer's own advert on an aggregator: the Personio board is the lead, and it brings that
# company's other postings with it. Hand-written, like every fixture here.
_AGGREGATOR_PAYLOAD = {
    "referenznummer": "DISCOVERY-1",
    "stellenangebotsTitel": "Prozessingenieur (m/w/d)",
    "firma": "Andere Firma GmbH",
    "externeURL": "https://anderefirma.jobs.personio.de/job/998877",
    "stellenangebotsBeschreibung": (
        "Bewerbung bitte über <a href='https://jobs.lever.co/drittefirma/abc'>unser Portal</a>. "
        "Mehr über uns auf https://www.linkedin.com/company/anderefirma"
    ),
    "stellenlokationen": [{"ort": "Wien", "land": "Österreich"}],
}


async def _ingest_aggregator_posting() -> None:
    from trouveur.db.queries import ingest as iq
    from trouveur.ingest.persist import persist

    document = RawDocument(
        source="arbeitsagentur",
        external_id=_AGGREGATOR_PAYLOAD["referenznummer"],
        kind=DocumentKind.LISTING,
        payload=_AGGREGATOR_PAYLOAD,
    )
    async with connect() as conn:
        await iq.archive_documents(conn, [document])
        await persist(
            conn, "arbeitsagentur", [document.external_id], requires_detail=True
        )


async def _leads() -> list[dict]:
    async with connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT url, host, result::text AS result, source, scope, resolve_version, job_id "
            "FROM discovery_lead ORDER BY url"
        )
        return [dict(row._mapping) for row in rows]


async def _tenants() -> list[dict]:
    async with connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT source, scope, enabled, origin::text AS origin, dropped_at "
            "FROM source_tenant ORDER BY source, scope"
        )
        return [dict(row._mapping) for row in rows]


async def test_mining_an_aggregator_posting_proposes_the_employers_own_board(clean_db):
    """The whole chain on one posting: a lead per link, the board as a disabled candidate.

    Through the real ingest path rather than an INSERT, because what is being tested is that the
    link survives normalisation -- the Lever link exists only as an `href` in the raw payload, and
    the stored description has had it stripped out.
    """
    await _ingest_aggregator_posting()
    report = await discovery.run_once()

    assert report.mined == 1
    leads = await _leads()
    by_url = {lead["url"]: lead for lead in leads}

    personio = by_url["https://anderefirma.jobs.personio.de/job/998877"]
    assert (personio["source"], personio["scope"]) == ("personio", "anderefirma")
    assert personio["result"] == "resolved"
    assert personio["job_id"] is not None

    lever = by_url["https://jobs.lever.co/drittefirma/abc"]
    assert (lever["source"], lever["scope"]) == ("lever", "drittefirma")

    assert "https://www.linkedin.com/company/anderefirma" not in by_url, (
        "a company's social links are not leads; kept, they outnumber the real ones"
    )

    proposed = {(row["source"], row["scope"]): row for row in await _tenants()}
    assert set(proposed) == {("personio", "anderefirma"), ("lever", "drittefirma")}
    for row in proposed.values():
        assert row["enabled"] is False, (
            "discovery must never enlarge the crawl: a human promotes a candidate"
        )
        assert row["origin"] == TenantOrigin.DISCOVERED.value


async def test_a_second_pass_finds_nothing_and_writes_nothing(clean_db):
    """Mining is marked per posting, so the walk terminates. Without it the runner re-reads the
    whole archive every tick for ever and the leads table never stops being rewritten."""
    await _ingest_aggregator_posting()
    first = await discovery.run_once()
    second = await discovery.run_once()

    assert first.leads == 2
    assert (second.mined, second.leads, second.rechecked, second.candidates) == (0, 0, 0, 0)
    assert len(await _leads()) == 2


async def test_bumping_the_mine_version_reads_the_archive_again(clean_db, monkeypatch):
    """A wider extractor has to be able to go back over postings already mined, and the raw
    archive is what makes that free. This is the repair path for every lead we did not notice."""
    await _ingest_aggregator_posting()
    await discovery.run_once()
    async with connect() as conn:
        await conn.exec_driver_sql("DELETE FROM discovery_lead")

    monkeypatch.setattr(versions, "MINE_VERSION", versions.MINE_VERSION + 1)
    report = await discovery.run_once()

    assert report.mined == 1
    assert report.leads == 2


async def test_bumping_the_resolve_version_rereads_every_lead(clean_db, monkeypatch):
    """The reason a lead keeps its raw URL: the day an adapter for a new platform ships, a bump
    turns every link we have already seen into a board, with no new request to anybody."""
    await _ingest_aggregator_posting()
    await discovery.run_once()
    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE discovery_lead SET result = 'unknown_host', source = NULL, scope = NULL"
        )

    monkeypatch.setattr(versions, "RESOLVE_VERSION", versions.RESOLVE_VERSION + 1)
    report = await discovery.run_once()

    assert report.rechecked == 2
    assert {lead["source"] for lead in await _leads()} == {"personio", "lever"}
    assert all(
        lead["resolve_version"] == versions.RESOLVE_VERSION for lead in await _leads()
    )


async def test_promoting_never_overwrites_a_decision_somebody_made(clean_db):
    """A board an operator enabled must stay enabled, and one they dropped must stay dropped.

    Both would fail silently the other way round: a re-enabled drop comes back as a candidate on
    every pass and is rejected again, and a board switched off by discovery stops being swept
    while the crawl set still lists it.
    """
    async with connect() as conn:
        await admin_q.add_tenants(conn, "personio", ["anderefirma"], enabled=True)
        await admin_q.add_tenants(conn, "lever", ["drittefirma"], enabled=False)
        await admin_q.drop_tenant(conn, "lever", "drittefirma", note="tried, nothing in our area")

    await _ingest_aggregator_posting()
    await discovery.run_once()

    rows = {(row["source"], row["scope"]): row for row in await _tenants()}
    assert rows[("personio", "anderefirma")]["enabled"] is True
    assert rows[("personio", "anderefirma")]["origin"] == TenantOrigin.MANUAL.value
    assert rows[("lever", "drittefirma")]["dropped_at"] is not None


async def test_the_coverage_report_reads_the_leads_back(clean_db):
    """The panel an operator and an agent both read. An empty section is absent rather than zero,
    which is what keeps a key from reading as an answer."""
    from trouveur.coverage import leads as lead_coverage

    async with connect() as conn:
        assert await lead_coverage(conn) is None

    await _ingest_aggregator_posting()
    await discovery.run_once()

    async with connect() as conn:
        card = await lead_coverage(conn)
    assert card is not None
    assert (card.leads, card.resolved, card.unread) == (2, 2, 0)
    assert card.unmined_jobs == 0


async def test_a_posting_files_no_lead_for_the_board_it_came_from(clean_db, gh_board):
    """On its own board every Greenhouse posting would file a lead naming that board, one row per
    posting for ever, and the few leads pointing somewhere new would be lost among them."""
    from trouveur.db.queries import ingest as iq
    from trouveur.ingest.persist import persist
    from trouveur.sources.greenhouse import external_id

    documents = [
        RawDocument(
            source="greenhouse", external_id=external_id("beispiel", job["id"]),
            kind=DocumentKind.LISTING, scope="beispiel", payload=job,
        )
        for job in gh_board["jobs"]
    ]
    async with connect() as conn:
        await iq.archive_documents(conn, documents)
        await persist(
            conn, "greenhouse", [doc.external_id for doc in documents], requires_detail=False
        )

    await discovery.run_once()

    own = [
        lead for lead in await _leads()
        if (lead["source"], lead["scope"]) == ("greenhouse", "beispiel")
    ]
    assert own == []


async def test_two_reports_with_no_link_are_two_leads(clean_db):
    """A reader can report a job they could not link to, and there is more than one such job.

    The unique index on (origin, url) is partial for exactly this: those rows all carry the empty
    string, so a plain index would keep the first report and drop every one after it without
    saying anything.
    """
    from trouveur.models import Lead, LeadOrigin

    written = await discovery.record_leads(
        [
            Lead(origin=LeadOrigin.USER_REPORT, company="Eine GmbH", title="Statiker"),
            Lead(origin=LeadOrigin.USER_REPORT, company="Zwei GmbH", title="Bauleiter"),
        ]
    )
    assert written == 2
    leads = await _leads()
    assert [lead["result"] for lead in leads] == ["no_url", "no_url"]


async def test_the_same_link_reported_twice_is_one_lead(clean_db):
    """The lead is about the link, so a second sighting carries nothing new to store."""
    from trouveur.models import Lead, LeadOrigin

    lead = Lead(origin=LeadOrigin.USER_REPORT, url="https://jobs.lever.co/eine/1")
    assert await discovery.record_leads([lead]) == 1
    assert await discovery.record_leads([lead]) == 0


async def test_a_lead_from_outside_the_runner_proposes_its_board_at_once(clean_db):
    """A reader reporting a job gets its board into the crawl set without waiting for a tick.

    The runner's own pass promotes only when something moved in it, so a lead written by a web
    route would otherwise sit resolved and unproposed until unrelated work came along.
    """
    from trouveur.models import Lead, LeadOrigin

    await discovery.record_leads(
        [Lead(origin=LeadOrigin.USER_REPORT, url="https://jobs.lever.co/gemeldet/1")]
    )
    assert [(row["source"], row["scope"], row["enabled"]) for row in await _tenants()] == [
        ("lever", "gemeldet", False)
    ]


async def test_two_postings_linking_one_board_do_not_break_the_batch(clean_db):
    """One INSERT carrying the same link twice must not raise, or the runner tick dies.

    Mining removes repeats WITHIN a posting, not across the page, and two adverts from the same
    employer naming its board is the ordinary case rather than a rare one. ON CONFLICT DO NOTHING
    tolerates it; DO UPDATE would raise "cannot affect row a second time", which is why this is
    pinned rather than left to whoever edits that statement next.
    """
    from trouveur.db.queries import discovery as q

    row = {
        "origin": "archive", "url": "https://jobs.lever.co/acme/1", "host": "jobs.lever.co",
        "job_id": None, "company": None, "title": None, "location_text": None,
        "result": "resolved", "source": "lever", "scope": "acme", "resolve_version": 1,
    }
    async with connect() as conn:
        assert await q.insert_leads(conn, [row, dict(row)]) == 1
