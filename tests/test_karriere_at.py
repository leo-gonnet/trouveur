"""karriere.at adapter tests, including the pagination trap.

Search pagination does not work: ?page=N, ?seite=N and /seite-N all return the same 15 rows.
A pagination loop would silently re-fetch page 1 forever, so the adapter must not build one.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from trouveur.models import ProfileData
from trouveur.sources.http import PoliteClient
from trouveur.sources.karriere_at import KarriereAtSource

FIXTURES = Path(__file__).parent / "fixtures"

def _listing_row(job_id: str, title: str) -> str:
    return (
        f'<a class="m-jobsListItem__titleLink" '
        f'href="https://www.karriere.at/jobs/{job_id}" target="_blank">{title}</a>'
    )


LISTING = "<html><body>" + "".join(
    _listing_row(job_id, title)
    for job_id, title in [
        ("111", "Wirtschaftsingenieur (m/w/d)"),
        ("222", "Praktikum Logistik"),
        ("333", "Industrial Engineer &amp; Planner"),
    ]
) + "</body></html>"


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.detail = (FIXTURES / "jobposting.html").read_text(encoding="utf-8")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/jobs/") and path.split("/")[-1].isdigit():
            return httpx.Response(200, text=self.detail)
        return httpx.Response(200, text=LISTING)


@pytest.fixture
def transport():
    return FakeTransport()


async def _run(source: KarriereAtSource, transport: FakeTransport):
    client = PoliteClient(delay=0)
    client._client = httpx.AsyncClient(transport=transport, base_url="https://www.karriere.at")
    jobs = [job async for job in source.fetch(client, None)]
    await client._client.aclose()
    return jobs


async def test_extracts_id_and_title_from_the_listing(transport):
    source = KarriereAtSource(keywords=["wirtschaftsingenieur"])
    client = PoliteClient(delay=0)
    client._client = httpx.AsyncClient(transport=transport)
    hits = await source._search(client, "wirtschaftsingenieur")
    await client._client.aclose()
    assert hits == [
        ("111", "Wirtschaftsingenieur (m/w/d)"),
        ("222", "Praktikum Logistik"),
        ("333", "Industrial Engineer & Planner"),
    ]


async def test_unescapes_html_entities_in_titles(transport):
    source = KarriereAtSource(keywords=["x"])
    client = PoliteClient(delay=0)
    client._client = httpx.AsyncClient(transport=transport)
    hits = await source._search(client, "x")
    await client._client.aclose()
    assert "&amp;" not in dict(hits)["333"]


async def test_title_filter_prevents_the_detail_request(transport):
    """The point of the title gate: an obvious miss must not cost an HTTP request."""
    profile = ProfileData(keywords=["Wirtschaftsingenieur"])
    from trouveur.filters.rules import title_is_plausible

    source = KarriereAtSource(
        keywords=["wirtschaftsingenieur"],
        title_filter=lambda t: title_is_plausible(t, profile),
    )
    await _run(source, transport)
    fetched = [r.url.path for r in transport.requests if r.url.path.split("/")[-1].isdigit()]
    assert "/jobs/222" not in fetched, "Praktikum should have been rejected before fetching"
    assert "/jobs/111" in fetched


async def test_never_requests_more_than_one_search_page_per_keyword(transport):
    """Pagination does not work on this site; a loop would re-fetch page 1 forever."""
    source = KarriereAtSource(keywords=["wirtschaftsingenieur", "industrial-engineer"])
    await _run(source, transport)
    searches = [r for r in transport.requests if not r.url.path.split("/")[-1].isdigit()]
    assert len(searches) == 2
    for request in searches:
        assert "page" not in request.url.params
        assert "seite" not in request.url.params


async def test_sitemap_sweep_is_off_by_default(transport):
    source = KarriereAtSource(keywords=["x"])
    await _run(source, transport)
    assert not any("sitemap" in str(r.url) for r in transport.requests)


async def test_deduplicates_ids_seen_under_several_keywords(transport):
    source = KarriereAtSource(keywords=["a", "b", "c"])
    jobs = await _run(source, transport)
    assert len({j.source_native_id for j in jobs}) == len(jobs)


async def test_parses_detail_pages_into_jobs(transport):
    source = KarriereAtSource(keywords=["wirtschaftsingenieur"])
    jobs = await _run(source, transport)
    assert jobs
    assert jobs[0].source == "karriere_at"
    assert jobs[0].location_country == "AT"
