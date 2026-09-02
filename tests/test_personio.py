from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from trouveur.sources.http import PoliteClient
from trouveur.sources.personio import PersonioSource, parse_tenant, parse_tenants

FIXTURES = Path(__file__).parent / "fixtures"


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.get(request.url.host, httpx.Response(404))


@pytest.fixture
def xml() -> str:
    return (FIXTURES / "personio.xml").read_text(encoding="utf-8")


async def _run(tenants: list[str], transport: FakeTransport):
    client = PoliteClient(delay=0)
    client._client = httpx.AsyncClient(transport=transport)
    jobs = [job async for job in PersonioSource(tenants=tenants).fetch(client, None)]
    await client._client.aclose()
    return jobs


async def test_parses_positions_from_the_feed(xml):
    transport = FakeTransport({"good.jobs.personio.de": httpx.Response(200, text=xml)})
    jobs = await _run(["good"], transport)
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Wirtschaftsingenieur (m/w/d)"
    assert first.company == "Beispiel SE & Co. KG"
    assert first.location_city == "Wien"
    assert first.source_native_id == "good:1834171"
    assert first.url == "https://good.jobs.personio.de/job/1834171"


async def test_joins_description_sections(xml):
    transport = FakeTransport({"good.jobs.personio.de": httpx.Response(200, text=xml)})
    jobs = await _run(["good"], transport)
    assert "Prozessoptimierung" in jobs[0].description
    assert "Abgeschlossenes Studium" in jobs[0].description


async def test_position_without_id_is_skipped(xml):
    transport = FakeTransport({"good.jobs.personio.de": httpx.Response(200, text=xml)})
    jobs = await _run(["good"], transport)
    assert all(j.title != "Broken row with no id" for j in jobs)


async def test_empty_job_descriptions_yield_none_not_an_error(xml):
    transport = FakeTransport({"good.jobs.personio.de": httpx.Response(200, text=xml)})
    jobs = await _run(["good"], transport)
    assert jobs[1].description is None


async def test_country_is_not_guessed_from_office(xml):
    """The feed states no country; inferring one from an office name would be worse than None."""
    transport = FakeTransport({"good.jobs.personio.de": httpx.Response(200, text=xml)})
    jobs = await _run(["good"], transport)
    assert all(j.location_country is None for j in jobs)


async def test_a_dead_tenant_does_not_stop_the_others(xml):
    transport = FakeTransport(
        {
            "dead.jobs.personio.de": httpx.Response(404),
            "redirecting.jobs.personio.de": httpx.Response(307),
            "good.jobs.personio.de": httpx.Response(200, text=xml),
        }
    )
    jobs = await _run(["dead", "redirecting", "good"], transport)
    assert len(jobs) == 2


async def test_unparseable_xml_is_skipped():
    transport = FakeTransport({"bad.jobs.personio.de": httpx.Response(200, text="<not xml")})
    assert await _run(["bad"], transport) == []


def test_parse_tenant_accepts_a_bare_slug():
    assert parse_tenant("acme-gmbh") == "acme-gmbh"
    assert parse_tenant("  ACME-GmbH  ") == "acme-gmbh"


def test_parse_tenant_extracts_the_slug_from_a_careers_url():
    # Bulk paste is the main entry path, and what people paste is what the browser showed them.
    for url in (
        "https://acme-gmbh.jobs.personio.de/",
        "https://acme-gmbh.jobs.personio.de/xml",
        "http://acme-gmbh.jobs.personio.de/job/12345",
        "acme-gmbh.jobs.personio.de",
    ):
        assert parse_tenant(url) == "acme-gmbh", url


def test_parse_tenant_rejects_unrelated_input():
    for value in ("", "   ", "https://example.com/jobs", "-leading-hyphen", "has space", "a/b"):
        assert parse_tenant(value) is None, value


def test_parse_tenants_splits_dedupes_and_reports_rejects():
    good, bad = parse_tenants(
        "acme-gmbh, musterfirma\nhttps://third.jobs.personio.de/xml\nacme-gmbh\nhttps://x.com/y"
    )
    assert good == ["acme-gmbh", "musterfirma", "third"]
    assert bad == ["https://x.com/y"]


def test_parse_tenants_does_not_split_on_spaces():
    # Splitting on spaces would turn one mistyped line into several plausible-looking tenants
    # instead of one visible rejection.
    good, bad = parse_tenants("not a real slug")
    assert good == []
    assert bad == ["not a real slug"]
