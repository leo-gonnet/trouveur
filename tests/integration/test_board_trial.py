"""The board trial against a real Postgres: one sweep decides whether a discovered board stays.

What only a database shows here is the verdict itself. It is `db/queries/location.py` run against
a candidate's own postings, so a board kept or turned away by a rule of its own would turn on
boards the matcher then filters out -- and nothing but a real filter over real facets can catch
that. The queue's cap and its interval are the other half: they are the whole of the cost control,
and a wrong comparison there sweeps the entire candidate pool every night.
"""

from __future__ import annotations

from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.ingest import workers
from trouveur.models import TenantOrigin

# Hand-written, like every fixture here. One board hiring where the reader is and one hiring on
# the other side of the world: the whole trial is telling those two apart.
_LOCAL_JOB = {
    "id": 7001,
    "title": "Verfahrensingenieur",
    "company_name": "Nahe GmbH",
    "absolute_url": "https://job-boards.greenhouse.io/nahegmbh/jobs/7001",
    "location": {"name": "Wien, Austria"},
    "offices": [{"id": 1, "name": "Austria", "location": None}],
    "departments": [{"id": 2, "name": "Operations"}],
    "first_published": "2026-09-01T10:00:00-04:00",
    "updated_at": "2026-09-02T10:00:00-04:00",
    "content": "<p>Prozessoptimierung in Wien.</p>",
}
_DISTANT_JOB = {
    "id": 7002,
    "title": "Process Engineer",
    "company_name": "Ferne KK",
    "absolute_url": "https://job-boards.greenhouse.io/fernekk/jobs/7002",
    "location": {"name": "Tokyo, Japan"},
    "offices": [{"id": 3, "name": "Japan", "location": None}],
    "departments": [{"id": 2, "name": "Operations"}],
    "first_published": "2026-09-01T10:00:00-04:00",
    "updated_at": "2026-09-02T10:00:00-04:00",
    "content": "<p>Process improvement in Tokyo.</p>",
}


# A posting derivation could not place: the company's own wording, and no office to fall back on.
# The location filter admits it, and the trial must not promote a board on it.
_UNPLACEABLE_JOB = {
    "id": 7003,
    "title": "Projektleiter",
    "company_name": "Nirgendwo AG",
    "absolute_url": "https://job-boards.greenhouse.io/nirgendwo/jobs/7003",
    "location": {"name": "Standort nach Absprache"},
    "offices": [],
    "departments": [{"id": 2, "name": "Operations"}],
    "first_published": "2026-09-01T10:00:00-04:00",
    "updated_at": "2026-09-02T10:00:00-04:00",
    "content": "<p>Projektleitung.</p>",
}


def _ok(scope: str):
    from trouveur.sources.base import ScopeResult

    return ScopeResult(scope=scope, ok=True, documents=1)


async def _candidate(scope: str) -> None:
    async with connect() as conn:
        await admin_q.add_tenants(
            conn, "greenhouse", [scope], enabled=False, origin=TenantOrigin.DISCOVERED
        )


async def _collect(scope: str, payload: dict) -> None:
    """Put one posting on a board through the real ingest path, facets and all.

    Through `persist` and the derive queue rather than an INSERT: the verdict reads `job_facet`,
    and facets a test wrote by hand would be a second derivation to keep in step with the real one.
    """
    from trouveur.db.queries import ingest as iq
    from trouveur.ingest.persist import persist
    from trouveur.models import DocumentKind, RawDocument
    from trouveur.sources.greenhouse import external_id

    document = RawDocument(
        source="greenhouse",
        external_id=external_id(scope, payload["id"]),
        kind=DocumentKind.LISTING,
        scope=scope,
        payload=payload,
    )
    async with connect() as conn:
        await iq.archive_documents(conn, [document])
        await persist(conn, "greenhouse", [document.external_id], requires_detail=False)
    async with connect() as conn:
        await workers.drain_derive(conn)


async def _tenant(scope: str) -> dict:
    async with connect() as conn:
        row = await conn.exec_driver_sql(
            "SELECT enabled, origin::text AS origin, dropped_at, decided_at "
            "FROM source_tenant "
            f"WHERE source = 'greenhouse' AND scope = '{scope}'"
        )
        return dict(row.one()._mapping)


async def test_a_board_with_a_posting_in_a_readers_area_is_swept_nightly_with_no_human(clean_db):
    """The point of the whole task: discovery proposes, the sweep decides, nobody reads a list."""
    from tests.integration.seed import seed_user
    from trouveur.discovery import trial

    await seed_user()
    await _candidate("nahegmbh")
    await _collect("nahegmbh", _LOCAL_JOB)

    assert await trial.judge() == 1
    assert (await _tenant("nahegmbh"))["enabled"] is True


async def test_a_board_hiring_nowhere_near_a_reader_is_left_off_the_nightly_sweep(clean_db):
    """Not a rejection -- the row stays a candidate and is asked again after the interval. But it
    must not cost a request a night, which is the only reason the trial can be afforded at all."""
    from tests.integration.seed import seed_user
    from trouveur.discovery import trial

    await seed_user()
    await _candidate("fernekk")
    await _collect("fernekk", _DISTANT_JOB)

    assert await trial.judge() == 0
    row = await _tenant("fernekk")
    assert row["enabled"] is False
    assert row["dropped_at"] is None, "a board with nothing local was not decided against"


async def test_the_trial_never_switches_a_sweeping_board_off(clean_db):
    """The crawl set only ever grows. A company with nothing open in the area today is the company
    that posts there next month, and a set that shrinks on its own shrinks silently."""
    from tests.integration.seed import seed_user
    from trouveur.discovery import trial

    await seed_user()
    async with connect() as conn:
        await admin_q.add_tenants(
            conn, "greenhouse", ["fernekk"], enabled=True, origin=TenantOrigin.DISCOVERED
        )
    await _collect("fernekk", _DISTANT_JOB)

    await trial.judge()
    assert (await _tenant("fernekk"))["enabled"] is True


async def test_the_trial_does_not_undo_a_board_an_operator_dropped(clean_db):
    """A board somebody tried and decided against must stay decided. Otherwise its own old
    postings hand it back to the sweep and the decision is undone with nothing recording it."""
    from tests.integration.seed import seed_user
    from trouveur.discovery import trial

    await seed_user()
    await _candidate("nahegmbh")
    async with connect() as conn:
        await admin_q.drop_tenant(conn, "greenhouse", "nahegmbh", note="not wanted")
    await _collect("nahegmbh", _LOCAL_JOB)

    assert await trial.judge() == 0
    row = await _tenant("nahegmbh")
    assert row["enabled"] is False
    assert row["dropped_at"] is not None


async def test_the_trial_does_not_undo_a_board_an_operator_disabled(clean_db):
    """`disable` and `drop` are different decisions and both are decisions. A disabled board was
    indistinguishable from a candidate nothing had asked, so the trial swept it, found its own
    local posting, and switched it back on -- every night, with nothing on any page saying so."""
    from tests.integration.seed import seed_user
    from trouveur.db.queries import coverage as coverage_q
    from trouveur.discovery import trial

    await seed_user()
    async with connect() as conn:
        await admin_q.add_tenants(
            conn, "greenhouse", ["nahegmbh"], enabled=True, origin=TenantOrigin.DISCOVERED
        )
        await admin_q.set_tenant_enabled(conn, "greenhouse", "nahegmbh", False)
    await _collect("nahegmbh", _LOCAL_JOB)

    async with connect() as conn:
        due = await admin_q.tenants_due_for_trial(
            conn, limit=10, recheck_days=30, sources=["greenhouse"]
        )
    assert due == {}, "a board an operator switched off is not a board waiting for a trial"

    assert await trial.judge() == 0
    row = await _tenant("nahegmbh")
    assert row["enabled"] is False
    assert row["decided_at"] is not None

    async with connect() as conn:
        states = {row.source: row for row in await coverage_q.tenant_states(conn)}
    assert states["greenhouse"].disabled == 1, (
        "and the operator can see it: counted where a person's decision belongs, not as a "
        "candidate still waiting"
    )
    assert states["greenhouse"].to_try == 0


async def test_a_reader_who_named_no_location_does_not_promote_every_candidate(clean_db):
    """An empty location is "anywhere" to the filter: right for that reader, and no verdict at all
    on a board. One blank Profile would enable the whole pool for everybody, permanently."""
    from tests.integration.seed import seed_user
    from trouveur.discovery import trial

    await seed_user(countries=[], city_ids=[])
    await _candidate("fernekk")
    await _collect("fernekk", _DISTANT_JOB)

    assert await trial.judge() == 0
    assert (await _tenant("fernekk"))["enabled"] is False


async def test_a_posting_nothing_could_place_does_not_promote_its_board(clean_db):
    """The filter admits an unplaceable posting on purpose: being wrong costs one row ranked too
    high. Here it costs a board swept nightly for ever, so the trial wants a posting placed."""
    from tests.integration.seed import seed_user
    from trouveur.discovery import trial

    await seed_user()
    await _candidate("nirgendwo")
    await _collect("nirgendwo", _UNPLACEABLE_JOB)

    async with connect() as conn:
        facets = (
            await conn.exec_driver_sql(
                "SELECT f.countries, f.unplaced_countries, f.work_mode::text AS work_mode "
                "FROM job_facet f JOIN job j ON j.id = f.job_id WHERE j.scope = 'nirgendwo'"
            )
        ).one()
    assert not facets.countries and not facets.unplaced_countries, (
        "the fixture has to be a posting derivation could not place, or this proves nothing"
    )
    assert facets.work_mode != "remote"

    assert await trial.judge() == 0
    assert (await _tenant("nirgendwo"))["enabled"] is False


async def test_a_disabled_sources_candidates_do_not_spend_the_trial_cap(clean_db):
    """A source nothing will ask must be out of the queue, not merely out of the sweep: its boards
    are never stamped, so a pool of them bigger than the cap starves every other source."""
    async with connect() as conn:
        await admin_q.add_tenants(
            conn, "greenhouse", ["eins", "zwei"], enabled=False,
            origin=TenantOrigin.DISCOVERED,
        )
        await admin_q.add_tenants(
            conn, "personio", ["drei"], enabled=False, origin=TenantOrigin.DISCOVERED
        )
        due = await admin_q.tenants_due_for_trial(
            conn, limit=1, recheck_days=30, sources=["personio"]
        )
    assert due == {"personio": ["drei"]}, (
        "greenhouse sorts first and would take the only slot every night"
    )


async def test_a_failing_candidate_does_not_crowd_the_boards_needing_attention(clean_db):
    """Discovery misreads slugs by the thousand and each 404 grows a streak for ever. Ordered by
    streak they bury the swept boards the page exists for, and no candidate needs a decision."""
    from trouveur.sources.base import ScopeResult

    async with connect() as conn:
        await admin_q.add_tenants(conn, "greenhouse", ["beispiel"], enabled=True)
        await _candidate("tippfehler")
        await admin_q.record_scope_health(
            conn,
            "greenhouse",
            [
                ScopeResult(scope="beispiel", ok=False, documents=0, error="HTTP 500"),
                ScopeResult(scope="tippfehler", ok=False, documents=0, error="HTTP 404"),
            ],
        )
        rows, totals = await admin_q.tenants_needing_attention(conn, limit=50)

    assert [row.scope for row in rows] == ["beispiel"]
    assert totals["failing"] == 1
    assert totals["candidates"] == 1, "still counted, so the operator knows the pool is there"


async def test_the_trial_queue_respects_its_cap_and_then_its_interval(clean_db):
    """The cap is the scan's whole protection: without it the pool is swept every night, and at
    the size discovery reaches that is hours. The interval is what stops a board being re-asked
    before there is any chance of a new answer."""
    await _candidate("eins")
    await _candidate("zwei")
    await _candidate("drei")

    async with connect() as conn:
        first = await admin_q.tenants_due_for_trial(
            conn, limit=2, recheck_days=30, sources=["greenhouse"]
        )
        assert sum(len(scopes) for scopes in first.values()) == 2
        await admin_q.record_trials(conn, first)

        second = await admin_q.tenants_due_for_trial(
            conn, limit=10, recheck_days=30, sources=["greenhouse"]
        )
        assert sum(len(scopes) for scopes in second.values()) == 1, (
            "a board tried tonight must not be in tomorrow's queue"
        )
        tried = set(first["greenhouse"])
        assert not tried & set(second["greenhouse"])
        await admin_q.record_trials(conn, second)

        assert await admin_q.tenants_due_for_trial(
            conn, limit=10, recheck_days=30, sources=["greenhouse"]
        ) == {}

        await conn.exec_driver_sql(
            "UPDATE source_scope_health SET trial_at = now() - interval '31 days'"
        )
        due = await admin_q.tenants_due_for_trial(
            conn, limit=10, recheck_days=30, sources=["greenhouse"]
        )
    assert sorted(due["greenhouse"]) == ["drei", "eins", "zwei"], (
        "after the interval every board that found nothing is asked again"
    )


async def test_a_sweep_keeps_the_trial_stamp_of_the_boards_it_did_not_touch(clean_db):
    """`prune_scope_health` runs after every sweep with only the scopes that sweep asked for.

    Reading an absent scope as an unregistered one would delete `trial_at` for every candidate
    not tried tonight, putting the whole pool back in tomorrow's queue -- with the cap as the only
    thing between that and sweeping all of it nightly. The same bug emptied the health of every
    board an operator had switched off.
    """
    await _candidate("eins")
    async with connect() as conn:
        await admin_q.record_trials(conn, {"greenhouse": ["eins"]})
        await admin_q.record_scope_health(conn, "greenhouse", [_ok("vergessen")])

        removed = await admin_q.prune_scope_health(conn, "greenhouse", ["beispiel"])
        rows = await conn.exec_driver_sql(
            "SELECT scope, trial_at FROM source_scope_health WHERE source = 'greenhouse'"
        )
        kept = {row.scope: row.trial_at for row in rows}

    assert removed == 1, "a scope nothing registers any more is still forgotten"
    assert "vergessen" not in kept
    assert kept["eins"] is not None


async def test_the_coverage_report_separates_boards_the_trial_turned_on(clean_db):
    """The number that answers "is discovery earning its keep": boards swept that no human added,
    and the postings in a reader's area that came from them."""
    from tests.integration.seed import seed_user
    from trouveur.coverage import report
    from trouveur.discovery import trial

    await seed_user()
    await _candidate("nahegmbh")
    await _candidate("fernekk")
    await _collect("nahegmbh", _LOCAL_JOB)
    await _collect("fernekk", _DISTANT_JOB)
    async with connect() as conn:
        await admin_q.record_trials(conn, {"greenhouse": ["nahegmbh", "fernekk"]})
    await trial.judge()

    async with connect() as conn:
        card = await report(conn, horizon_days=36500)

    tenants = {row.source: row for row in card.tenants}
    assert tenants["greenhouse"].auto_on == 1
    assert tenants["greenhouse"].monthly == 1
    assert tenants["greenhouse"].to_try == 0
    assert tenants["greenhouse"].sweeping == 1

    area = card.areas[0]
    assert area.discovered_jobs == 1, (
        "the Tokyo posting is in the corpus but not in this reader's area"
    )


class _StubClient:
    """Stand-in for PoliteClient. No request leaves the process, here or anywhere in the suite."""

    def __init__(self, boards: dict[str, list[dict]]) -> None:
        self.boards = boards
        self.requested: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, *, params=None, headers=None):
        import httpx

        self.requested.append(url)
        slug = url.rstrip("/").split("/")[-2]
        if slug not in self.boards:
            return httpx.Response(404, json={}, request=httpx.Request("GET", url))
        return httpx.Response(
            200, json={"jobs": self.boards[slug]}, request=httpx.Request("GET", url)
        )


async def test_a_sweep_fetches_its_trial_boards_and_marks_them_tried(clean_db, monkeypatch):
    """The wiring, through the real `ingest.run`: a candidate is fetched beside the enabled
    boards, no more than the cap are fetched, and every one asked is stamped.

    The stamp is what bounds everything else. Unstamped, tomorrow's queue holds the same boards
    and the cap becomes the number of boards swept per night rather than tried per night.
    """
    from trouveur.config import get_settings
    from trouveur.ingest import pipeline

    async with connect() as conn:
        await admin_q.add_tenants(conn, "greenhouse", ["beispiel"], enabled=True)
    await _candidate("nahegmbh")
    await _candidate("fernekk")
    await _candidate("spaeter")

    client = _StubClient(
        {
            "beispiel": [],
            "nahegmbh": [_LOCAL_JOB],
            "fernekk": [_DISTANT_JOB],
            "spaeter": [_LOCAL_JOB],
        }
    )
    monkeypatch.setattr(pipeline, "PoliteClient", lambda: client)
    settings = get_settings().model_copy(
        update={"trial_boards_per_run": 2, "trial_recheck_days": 30}
    )

    report = await pipeline.run(only_source="greenhouse", settings=settings)

    assert report.trialled == 2, "the cap is the scan's only protection"
    asked = {url.rstrip("/").split("/")[-2] for url in client.requested}
    assert "beispiel" in asked, "the enabled boards are still swept"
    assert len(asked & {"nahegmbh", "fernekk", "spaeter"}) == 2

    async with connect() as conn:
        due = await admin_q.tenants_due_for_trial(
            conn, limit=10, recheck_days=30, sources=["greenhouse"]
        )
    assert sum(len(scopes) for scopes in due.values()) == 1, (
        "a board asked tonight must not be asked again tomorrow"
    )
