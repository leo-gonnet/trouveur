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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def paced(self, delay):
        self.delay = delay
        return self

    async def get(self, url, *, params=None, headers=None):
        self.requested.append(url)
        raise SourceBlocked(f"{url} answered HTTP 429 after every retry.")


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
            "UPDATE source_sweep SET finished_at = now() - interval '2 hours', "
            "started_at = now() - interval '2 hours'"
        )
    await pipeline.run(settings=_only_greenhouse())
    assert len(client.requested) == 2, "after the pause the source is swept again"
    assert client.delay == get_settings().request_delay_seconds * 2, "and more slowly"
