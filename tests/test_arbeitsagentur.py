"""Adapter tests, including the regression guards AGENTS.md marks as mandatory.

The traps these cover all fail *silently* against the live API — zero rows or an HTTP 400, never
an exception — so without these tests a regression ships unnoticed.
"""

from __future__ import annotations

import httpx
import pytest

from trouveur.models import SalaryPeriod
from trouveur.sources import arbeitsagentur as aa
from trouveur.sources.arbeitsagentur import ArbeitsagenturSource
from trouveur.sources.http import PoliteClient


class RecordingTransport(httpx.AsyncBaseTransport):
    """Serves fixtures and records every request, so we can assert on the URLs we build."""

    def __init__(self, search: dict, detail: dict) -> None:
        self.search, self.detail = search, detail
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "jobdetails" in request.url.path:
            return httpx.Response(200, json=self.detail)
        page = int(request.url.params.get("page", 1))
        body = self.search if page == 1 else {"maxErgebnisse": 2, "ergebnisliste": []}
        return httpx.Response(200, json=body)


async def _collect(transport, cities=None, keywords=("Wirtschaftsingenieur",)):
    client = PoliteClient(delay=0)
    client._client = httpx.AsyncClient(transport=transport)
    source = ArbeitsagenturSource(keywords=list(keywords), cities=cities)
    jobs = [job async for job in source.fetch(client, None)]
    await client._client.aclose()
    return jobs, source, client


@pytest.fixture
def transport(search_payload, detail_payload):
    return RecordingTransport(search_payload, detail_payload)


async def test_parses_search_results(transport):
    jobs, _, _ = await _collect(transport)
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Wirtschaftsingenieur (m/w/d)"
    assert first.company == "Beispiel Technik GmbH"
    assert first.location_city == "München"
    assert first.location_country == "DE"
    assert first.remote is True
    assert first.salary_period == SalaryPeriod.YEAR
    assert first.source_native_id == "00000-aaaaaaaaaaaaaaaa-S"


async def test_maps_austria_country_code(transport):
    jobs, _, _ = await _collect(transport)
    assert jobs[1].location_country == "AT"


async def test_falls_back_to_jobsuche_url_when_external_url_missing(transport):
    jobs, _, _ = await _collect(transport)
    assert jobs[1].url.endswith("00000-bbbbbbbbbbbbbbbb-S")


# --- Mandatory regression guards (AGENTS.md > Testing) ---------------------------------------


async def test_search_uses_v6_and_never_v4(transport):
    """pc/v4/jobs returns 403. A silent rollback to v4 would kill every German result."""
    await _collect(transport)
    search_urls = [str(r.url) for r in transport.requests if "jobdetails" not in r.url.path]
    assert search_urls, "expected at least one search request"
    for url in search_urls:
        assert "/pc/v6/jobs" in url
        assert "/pc/v4/" not in url


async def test_detail_uses_v4_and_never_v6(transport):
    """The inverse trap: jobdetails is v4-only, v5 and v6 both return 403."""
    _, source, client = await _collect(transport)
    client._client = httpx.AsyncClient(transport=transport)
    await source.enrich(client, "00000-aaaaaaaaaaaaaaaa-S")
    await client._client.aclose()
    detail_urls = [str(r.url) for r in transport.requests if "jobdetails" in r.url.path]
    assert detail_urls
    for url in detail_urls:
        assert "/pc/v4/jobdetails/" in url
        assert "/pc/v6/" not in url


async def test_city_is_sent_as_literal_umlaut_not_transliterated(transport):
    """wo=München returns ~178 results; wo=Muenchen returns 0, with no error either way."""
    await _collect(transport, cities=["München"])
    url = next(str(r.url) for r in transport.requests if "jobdetails" not in r.url.path)
    assert "Muenchen" not in url, "city was transliterated; this silently returns zero results"
    assert "M%C3%BCnchen" in url or "München" in url


async def test_never_sends_parameters_that_return_400(transport):
    """pav and homeoffice both return HTTP 400; arbeitszeit=ho returns zero rows."""
    await _collect(transport, cities=["Wien"])
    for request in transport.requests:
        params = request.url.params
        assert "pav" not in params
        assert "homeoffice" not in params
        assert params.get("arbeitszeit") != "ho"


async def test_uses_wo_not_arbeitsort(transport):
    """arbeitsort=Wien returns 0 results; wo=Wien returns 6."""
    await _collect(transport, cities=["Wien"])
    request = next(r for r in transport.requests if "jobdetails" not in r.url.path)
    assert request.url.params.get("wo") == "Wien"
    assert "arbeitsort" not in request.url.params


async def test_reads_ergebnisliste_not_stellenangebote(transport):
    """Older docs name the list key `stellenangebote`; the live v6 API uses `ergebnisliste`."""
    jobs, _, _ = await _collect(transport)
    assert jobs, "no jobs parsed - the response list key may have been changed"


async def test_enrich_returns_description_and_staffing_flag(transport):
    _, source, client = await _collect(transport)
    client._client = httpx.AsyncClient(transport=transport)
    extra = await source.enrich(client, "00000-aaaaaaaaaaaaaaaa-S")
    await client._client.aclose()
    assert "Prozessoptimierung" in extra["description"]
    assert extra["is_staffing_agency"] is False


def test_module_never_references_v4_search_endpoint():
    assert "/pc/v6/jobs" in aa._SEARCH_URL
    assert "/pc/v4/jobdetails" in aa._DETAIL_URL
