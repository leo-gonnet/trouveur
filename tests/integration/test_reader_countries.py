"""A source swept by reader country, through the real `ingest.run` and a real profile table."""

from __future__ import annotations

import httpx

from tests.integration.seed import seed_user


class _EuresStub:
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def paced(self, delay, *, attempts=None):
        return self

    async def post_json(self, url, *, json=None, params=None, headers=None):
        self.asked.append(json["locationCodes"])
        return {"numberRecords": 0, "jvs": [], "facets": {}}

    async def get(self, url, *, params=None, headers=None):  # pragma: no cover - not reached
        return httpx.Response(404, request=httpx.Request("GET", url))


async def test_eures_is_asked_for_the_countries_readers_look_in_and_no_other(
    clean_db, monkeypatch
):
    """Its whole corpus is millions of postings, so it is swept only where somebody looks.

    Read from the profiles on every run: a reader who adds a country is served from the next
    night on, with no crawl-set row for anybody to remember to add.
    """
    from trouveur.ingest import pipeline

    await seed_user(countries=["PT", "GR"])
    stub = _EuresStub()
    monkeypatch.setattr(pipeline, "PoliteClient", lambda: stub)

    report = await pipeline.run(only_source="eures")

    assert sorted(stub.asked) == [["el"], ["pt"]]
    assert report.per_source["eures"].error is None
