"""Telling a site that refused us from a site with nothing new. No network: a stubbed transport."""

from __future__ import annotations

import httpx
import pytest

from trouveur.ingest.pipeline import SWEEP_PAUSE, pause_after, slowdown
from trouveur.sources import http
from trouveur.sources.arbeitsagentur.client import ArbeitsagenturSource
from trouveur.sources.board import sweep_boards
from trouveur.sources.errors import SOURCE_BURST, FetchError, SourceBlocked
from trouveur.sources.feed import require_list
from trouveur.sources.workday.client import WorkdaySource

_CLOUDFLARE_WALL = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    "<body><script>window._cf_chl_opt={cType:'managed'};</script></body></html>"
)
# An ordinary page served through Cloudflare carries this script too, and is no block.
_CLOUDFLARE_PAGE = (
    "<html><head><title>Karriere</title>"
    "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></head>"
    "<body>Unsere Stellen</body></html>"
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(http.asyncio, "sleep", instant)


def _client(handler) -> http.PoliteClient:
    client = http.PoliteClient(delay=0)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _html(status: int, body: str):
    return lambda request: httpx.Response(
        status, text=body, headers={"content-type": "text/html; charset=utf-8"}
    )


async def test_a_429_after_every_retry_is_a_block():
    client = _client(lambda request: httpx.Response(429))
    with pytest.raises(SourceBlocked):
        await client.get_json("https://api.example.com/jobs")


async def test_a_503_after_every_retry_is_a_failure_not_a_block():
    client = _client(lambda request: httpx.Response(503))
    with pytest.raises(FetchError) as raised:
        await client.get_json("https://traffit.example.com/jobs")
    assert not isinstance(raised.value, SourceBlocked)


async def test_a_403_challenge_page_is_a_block():
    client = _client(_html(403, _CLOUDFLARE_WALL))
    with pytest.raises(SourceBlocked):
        await client.get("https://boards.example.com/acme")


async def test_a_challenge_page_where_json_was_expected_is_a_block():
    client = _client(_html(200, _CLOUDFLARE_WALL))
    with pytest.raises(SourceBlocked):
        await client.get_json("https://api.example.com/jobs")


async def test_an_empty_200_where_json_was_expected_is_a_block():
    client = _client(lambda request: httpx.Response(200, content=b""))
    with pytest.raises(SourceBlocked):
        await client.get_json("https://api.example.com/jobs")


async def test_an_ordinary_page_behind_cloudflare_is_no_block():
    client = _client(_html(200, _CLOUDFLARE_PAGE))
    response = await client.get("https://karriere.example.com/")
    assert response.status_code == 200


def test_a_really_empty_page_is_empty_and_a_page_without_its_rows_is_a_block():
    assert require_list({"jobs": [], "totalCount": 0}, "jobs", source="example") == []
    with pytest.raises(SourceBlocked):
        require_list({}, "jobs", source="example")


async def _sweep(statuses: dict[str, int]):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        scope = request.url.path.strip("/")
        requested.append(scope)
        return httpx.Response(statuses[scope], json={"jobs": [{"id": 1}]})

    async def sink(_documents):
        return None

    outcome = await sweep_boards(
        _client(handler),
        sink,
        source="example",
        scopes=list(statuses),
        url_for=lambda scope: f"https://boards.example.com/{scope}",
        extract=lambda payload: payload["jobs"],
        identify=lambda row: row["id"],
    )
    return outcome, requested


async def test_a_run_of_403s_stops_the_source_as_blocked():
    statuses = {f"board{n}": 403 for n in range(SOURCE_BURST + 3)}
    outcome, requested = await _sweep(statuses)
    assert outcome.blocked
    assert not outcome.ok
    assert len(requested) == SOURCE_BURST, "the boards after the block are not asked"


async def test_one_private_board_is_not_a_block():
    statuses = {"eins": 200, "privat": 403, "zwei": 200}
    outcome, requested = await _sweep(statuses)
    assert outcome.blocked is None
    assert outcome.closable_scopes == ["eins", "zwei"]
    assert requested == ["eins", "privat", "zwei"]


async def _nowhere(_documents):
    return None


async def test_a_block_stops_a_partitioned_sweep_instead_of_failing_each_partition():
    asked = []

    def handler(request):
        if "berufsfeld" not in request.url.params:
            facet = {"berufsfeld": {"counts": {"Bau": 1, "Pflege": 1}}}
            return httpx.Response(200, json={"maxErgebnisse": 2, "facetten": facet})
        asked.append(request.url.params["berufsfeld"])
        return httpx.Response(429)

    with pytest.raises(SourceBlocked):
        await ArbeitsagenturSource().sweep(_client(handler), _nowhere)
    assert set(asked) == {"Bau"}, "the partitions after the block are not asked"


async def test_a_block_stops_a_workday_sweep_instead_of_failing_each_board():
    asked = []

    def handler(request):
        asked.append(request.url.host)
        return httpx.Response(429)

    boards = ["eins:wd3:Karriere", "zwei:wd3:Karriere"]
    with pytest.raises(SourceBlocked):
        await WorkdaySource(boards=boards).sweep(_client(handler), _nowhere)
    assert set(asked) == {"eins.wd3.myworkdayjobs.com"}, "the boards after the block are not asked"


def test_each_block_in_a_row_waits_longer_and_sweeps_slower():
    assert pause_after(1) == SWEEP_PAUSE, "a nightly source skips the next night"
    assert pause_after(3) == SWEEP_PAUSE * 4
    assert pause_after(50).days == 7
    assert [slowdown(n) for n in (0, 1, 2, 9)] == [1, 2, 4, 16]
