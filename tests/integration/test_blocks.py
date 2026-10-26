"""A blocked source against a real Postgres: recorded as blocked, shown, then paused."""

from __future__ import annotations

from trouveur.config import get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.ingest import pipeline
from trouveur.sources.errors import SourceBlocked
from trouveur.sources.registry import SOURCES


class _WallClient:
    def __init__(self) -> None:
        self.requested: list[str] = []
        self.walled = True
        self.delay = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def paced(self, delay):
        self.delay = delay
        return self

    async def get(self, url, *, params=None, headers=None):
        import httpx

        self.requested.append(url)
        if self.walled:
            raise SourceBlocked(f"{url} answered HTTP 429 after every retry.")
        return httpx.Response(200, json={"jobs": []}, request=httpx.Request("GET", url))


def _only_greenhouse():
    others = ",".join(name for name in SOURCES if name != "greenhouse")
    return get_settings().model_copy(update={"disabled_sources": others})


async def test_a_blocked_sweep_is_recorded_shown_and_then_paused(clean_db, monkeypatch):
    from trouveur.coverage import report

    async with connect() as conn:
        await admin_q.add_tenants(conn, "greenhouse", ["eins", "zwei"], enabled=True)
    client = _WallClient()
    monkeypatch.setattr(pipeline, "PoliteClient", lambda: client)

    first = await pipeline.run(settings=_only_greenhouse())
    assert first.per_source["greenhouse"].blocked
    assert len(client.requested) == 1, "the second board is not asked once the first is refused"

    async with connect() as conn:
        sweep = (await conn.exec_driver_sql(
            "SELECT ok, blocked FROM source_sweep WHERE source = 'greenhouse'"
        )).one()
        health = (await conn.exec_driver_sql(
            "SELECT blocked FROM source_scope_health WHERE scope = 'eins'"
        )).scalar_one()
        card = await report(conn, horizon_days=7)
    assert (sweep.ok, sweep.blocked, health) == (False, True, True)
    assert [row.source for row in card.blocked] == ["greenhouse"]

    second = await pipeline.run(settings=_only_greenhouse())
    assert "greenhouse" in second.paused
    assert len(client.requested) == 1, "a paused source is not asked again"

    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE source_sweep SET finished_at = now() - interval '2 days', "
            "started_at = now() - interval '2 days'"
        )
    client.walled = False
    slower = get_settings().request_delay_seconds * 2
    await pipeline.run(settings=_only_greenhouse())
    assert len(client.requested) == 3, "after the pause the source is swept again"
    assert client.delay == slower, "and more slowly"

    client.delay = None
    await pipeline.run(settings=_only_greenhouse())
    assert client.delay == slower, "one good sweep does not bring back the pace that was blocked"


async def test_a_block_on_a_trial_board_does_not_cost_the_enabled_boards_their_night(
    clean_db, monkeypatch
):
    from trouveur.models import TenantOrigin

    async with connect() as conn:
        await admin_q.add_tenants(conn, "greenhouse", ["zzz-ours"], enabled=True)
        await admin_q.add_tenants(
            conn, "greenhouse", ["aaa-trial"], enabled=False, origin=TenantOrigin.DISCOVERED
        )
    client = _WallClient()
    monkeypatch.setattr(pipeline, "PoliteClient", lambda: client)

    await pipeline.run(settings=_only_greenhouse())
    assert "zzz-ours" in client.requested[0], "the board we sweep is asked before the trial"


async def test_a_block_marks_tried_only_the_trial_boards_that_answered(clean_db, monkeypatch):
    from trouveur.models import TenantOrigin

    trials = ["t1", "t2", "t3", "t4"]
    async with connect() as conn:
        await admin_q.add_tenants(conn, "greenhouse", ["zzz-ours"], enabled=True)
        await admin_q.add_tenants(
            conn, "greenhouse", trials, enabled=False, origin=TenantOrigin.DISCOVERED
        )

    class _WallAtT2(_WallClient):
        async def get(self, url, *, params=None, headers=None):
            self.walled = "/t2/" in url
            return await super().get(url, params=params, headers=headers)

    client = _WallAtT2()
    monkeypatch.setattr(pipeline, "PoliteClient", lambda: client)
    await pipeline.run(settings=_only_greenhouse())

    async with connect() as conn:
        tried = (await conn.exec_driver_sql(
            "SELECT scope FROM source_scope_health WHERE trial_at IS NOT NULL ORDER BY scope"
        )).scalars().all()
        due = await admin_q.tenants_due_for_trial(
            conn, limit=10, recheck_days=30, sources=["greenhouse"]
        )
    assert tried == ["t1"]
    assert sorted(due["greenhouse"]) == ["t2", "t3", "t4"]
