"""The weekly freehire pass against a real Postgres, over hand-written API pages."""

from __future__ import annotations

from trouveur.db.engine import connect
from trouveur.discovery import freehire


class _Stub:
    def __init__(self) -> None:
        self.asked: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_json(self, url, *, params=None, headers=None):
        self.asked.append(dict(params or {}))
        if url.endswith("/jobs/facets"):
            return {"data": {"facets": {"source": {"greenhouse": 2}}}}
        return {
            "data": [
                {
                    "source": "greenhouse", "external_id": f"nahegmbh:{n}",
                    "company": "Nahe GmbH", "title": "Prozessingenieur", "location": "Wien, at",
                    "url": f"https://job-boards.greenhouse.io/nahegmbh/jobs/{n}",
                }
                for n in (1, 2)
            ],
            "meta": {"total": 2},
        }


async def test_the_weekly_pass_files_one_lead_per_board_and_waits_a_week(clean_db, monkeypatch):
    from tests.integration.seed import seed_user
    from trouveur.coverage import leads

    await seed_user(countries=["AT"])
    stub = _Stub()
    monkeypatch.setattr(freehire, "PoliteClient", lambda: stub)

    found = await freehire.run()
    assert found is not None and len(found.leads) == 1
    assert {params.get("countries") for params in stub.asked} == {"AT"}

    async with connect() as conn:
        lead = (await conn.exec_driver_sql(
            "SELECT origin::text, source, scope, payload->>'board' FROM discovery_lead"
        )).one()
        tenant = (await conn.exec_driver_sql(
            "SELECT enabled, origin::text FROM source_tenant WHERE scope = 'nahegmbh'"
        )).one()
        sweep = (await conn.exec_driver_sql(
            "SELECT ok, blocked, documents_seen FROM source_sweep WHERE source = 'freehire'"
        )).one()
        card = await leads(conn)
    assert tuple(lead) == ("freehire", "greenhouse", "nahegmbh", "greenhouse:nahegmbh")
    assert tuple(tenant) == (False, "discovered"), "a board goes through the trial, never on"
    assert tuple(sweep) == (True, False, 2)
    assert [(row.label, row.boards) for row in card.origins] == [("freehire", 1)]

    assert await freehire.run() is None, "a second pass inside the week asks nothing"
    assert len(stub.asked) == 2
