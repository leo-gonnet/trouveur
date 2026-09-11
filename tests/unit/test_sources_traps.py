"""Regression guards for every trap the new adapters depend on.

Each behaviour here was verified against the live API on 2026-09-09 and each one fails *silently*
in production -- an empty board, a wrong date, a retried request that will never succeed. None
raises on its own, which is why they are pinned here rather than left to the type checker.

No network anywhere: every client is a stub.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from trouveur.sources.errors import SourceError


class StubClient:
    """Minimal stand-in for PoliteClient, recording what was asked for."""

    def __init__(self, responses=None, default=(404, None)) -> None:
        self.responses = responses or {}
        self.default = default
        self.requested: list[tuple[str, str, dict | None, dict | None]] = []

    def _respond(self, method, url, params, json):
        self.requested.append((method, url, params, json))
        entry = self.responses.get(url, self.default)
        if callable(entry):
            entry = entry(params, json)
        status, payload = entry
        request = httpx.Request(method, url)
        if isinstance(payload, str):
            return httpx.Response(
                status, text=payload, request=request, headers={"content-type": "text/html"}
            )
        if payload is None:
            return httpx.Response(status, text="", request=request)
        return httpx.Response(status, json=payload, request=request)

    async def get(self, url, *, params=None, headers=None):
        return self._respond("GET", url, params, None)

    async def post(self, url, *, json=None, params=None, headers=None):
        return self._respond("POST", url, params, json)

    async def get_json(self, url, *, params=None, headers=None):
        return self._respond("GET", url, params, None).json()


async def collect(source, client):
    seen = []

    async def sink(documents):
        seen.extend(documents)

    outcome = await source.sweep(client, sink)
    return outcome, seen


# --------------------------------------------------------------------------- politeness


def test_subdomain_tenants_share_one_politeness_budget():
    """Per-hostname throttling multiplies the budget by the number of tenants.

    Personio, Breezy and friends give every board its own subdomain. Keyed on the full hostname,
    a thousand boards would be a thousand independent 1 req/s budgets against one provider -- and
    every counter would still read as compliant.
    """
    from trouveur.sources.http import throttle_key

    assert throttle_key("https://alpha.jobs.personio.de/xml") == "personio.de"
    assert throttle_key("https://beta.jobs.personio.de/xml") == "personio.de"
    assert throttle_key("https://alpha.breezy.hr/json") == throttle_key(
        "https://beta.breezy.hr/json"
    )
    assert throttle_key("https://acme.wd5.myworkdayjobs.com/x") == "myworkdayjobs.com"
    # Distinct providers must not be merged into one queue.
    assert throttle_key("https://api.lever.co/v0/x") != throttle_key("https://api.ashbyhq.com/y")


# --------------------------------------------------------------------------- personio


def test_personio_unknown_tenant_is_a_bad_slug_not_throttling():
    """An unknown Personio tenant answers 429 with an HTML challenge page, never 404.

    PoliteClient retries 429 by design, so a typo'd slug would otherwise burn four attempts and a
    backoff on every sweep, for ever, and report itself in the health panel as rate limiting.
    """
    from trouveur.sources.personio import PersonioSource

    client = StubClient(
        {"https://ghost.jobs.personio.de/xml": (429, "<html>Vercel Security Checkpoint</html>")}
    )
    outcome, seen = asyncio.run(collect(PersonioSource(boards=["ghost"]), client))

    assert seen == []
    assert outcome.errors and "does not exist" in outcome.errors[0]
    # A board that failed must never be closable, or its postings are all retired.
    assert outcome.closable_scopes == []


def test_personio_builds_the_posting_url_because_the_feed_states_none():
    """There is no href, link or url element anywhere in the feed."""
    from trouveur.sources.personio import normalize

    job = normalize({"id": "42", "name": "Data Engineer"}, external_id="beispiel:42")

    assert job is not None
    assert job.url == "https://beispiel.jobs.personio.de/job/42"


def test_personio_repeated_xml_elements_decode_to_a_list():
    """One office and several offices must decode to the same shape, or one of them is misread."""
    from xml.etree import ElementTree

    from trouveur.sources.personio.client import element_to_dict

    node = ElementTree.fromstring(
        "<position><id>1</id><jobDescriptions>"
        "<jobDescription><name>A</name><value>x</value></jobDescription>"
        "<jobDescription><name>B</name><value>y</value></jobDescription>"
        "</jobDescriptions></position>"
    )
    record = element_to_dict(node)

    assert isinstance(record["jobDescriptions"]["jobDescription"], list)
    assert len(record["jobDescriptions"]["jobDescription"]) == 2


# --------------------------------------------------------------------------- workday


def test_workday_never_requests_a_page_larger_than_twenty():
    """limit=21 and above return HTTP 400. Raising it "for throughput" fails the whole sweep."""
    from trouveur.sources.workday import WorkdaySource
    from trouveur.sources.workday.client import PAGE_SIZE

    assert PAGE_SIZE == 20
    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
    client = StubClient({url: (200, {"total": 2000, "jobPostings": []})})
    asyncio.run(collect(WorkdaySource(boards=["acme:wd5:Careers"]), client))

    assert client.requested, "no request was made; the guard is not checking anything"
    for _method, _url, _params, body in client.requested:
        assert body["limit"] <= 20


def test_workday_pagination_stops_on_an_empty_page_not_on_total():
    """`total` contradicts itself across pages while rows keep arriving.

    Probed live: the same query reported total=2000 at offset 0, total=0 at offset 1980 and
    total=2000 at offset 2000, still returning rows past its own stated total. Terminating on it
    would stop early or never stop.
    """
    from trouveur.sources.workday import WorkdaySource

    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
    pages = [
        # A total of 0 alongside real rows: the walk must keep going.
        (200, {"total": 0, "jobPostings": [{"externalPath": "/job/a", "title": "A"}]}),
        (200, {"total": 2000, "jobPostings": [{"externalPath": "/job/b", "title": "B"}]}),
        (200, {"total": 2000, "jobPostings": []}),
    ]
    calls = {"n": 0}

    def respond(_params, _body):
        page = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return page

    outcome, seen = asyncio.run(
        collect(WorkdaySource(boards=["acme:wd5:Careers"]), StubClient({url: respond}))
    )

    assert [document.external_id for document in seen] == [
        "acme:wd5:Careers:/job/a",
        "acme:wd5:Careers:/job/b",
    ]
    assert outcome.closable_scopes == ["acme:wd5:Careers"]


def test_workday_relative_posted_on_never_becomes_a_date():
    """`postedOn` is prose ("Posted Today"). The detail's startDate is the only real date."""
    from trouveur.sources.workday import normalize

    listing = {
        "title": "Engineer",
        "postedOn": "Posted 30+ Days Ago",
        "locationsText": "3 Locations",
    }
    job = normalize(listing, None, external_id="acme:wd5:Careers:/job/x")

    assert job is not None
    assert job.posted_at is None

    dated = normalize(
        listing,
        {"jobPostingInfo": {"title": "Engineer", "startDate": "2026-09-01", "posted": True}},
        external_id="acme:wd5:Careers:/job/x",
    )
    assert dated is not None
    assert dated.posted_at == datetime(2026, 9, 1, tzinfo=UTC)


def test_workday_locations_text_is_a_count_not_a_place():
    """`locationsText` reads "3 Locations"; used as a location it fills the city column with it."""
    from trouveur.sources.workday import normalize

    job = normalize(
        {"title": "Engineer", "locationsText": "3 Locations"},
        None,
        external_id="acme:wd5:Careers:/job/x",
    )

    assert job is not None
    assert [location.raw for location in job.locations] == []


# --------------------------------------------------------------------------- lever


def test_lever_reads_the_title_from_text_and_the_date_from_milliseconds():
    """Lever has no `title` key, and `createdAt` is milliseconds where others state seconds."""
    from trouveur.sources.lever import normalize

    job = normalize(
        {
            "id": "abc",
            "text": "Data Engineer",
            "createdAt": 1787961600000,
            "hostedUrl": "https://jobs.lever.co/acme/abc",
        },
        external_id="acme:abc",
    )

    assert job is not None
    assert job.title == "Data Engineer"
    # Read as seconds this would land tens of thousands of years in the future.
    assert job.posted_at is not None
    assert job.posted_at.year == 2026


@pytest.mark.parametrize(
    ("module", "payload"),
    [
        ("lever", [{"id": "1", "text": "A"}]),
        ("breezy", [{"id": "1", "name": "A"}]),
        ("rippling", [{"uuid": "1", "name": "A"}]),
    ],
)
def test_bare_array_boards_are_read_as_the_list_itself(module, payload):
    """These three return a top-level array; reaching for a `jobs` key reports an empty board."""
    from importlib import import_module

    extract = import_module(f"trouveur.sources.{module}.client")._extract
    assert len(extract(payload)) == 1
    # An envelope appearing later must show up as an empty sweep, not a TypeError mid-batch.
    assert extract({"jobs": payload}) == []


# --------------------------------------------------------------------------- global feeds


def test_workable_never_sends_a_page_size():
    """An unsupported `limit` returns HTTP 200 with no jobs and no cursor.

    That is indistinguishable from the end of the corpus, so a sweep would silently collect
    nothing and report success.
    """
    from trouveur.sources.workable import WorkableSource

    client = StubClient(default=(200, {"jobs": [], "totalSize": 0}))
    asyncio.run(collect(WorkableSource(), client))

    assert client.requested
    for _method, _url, params, _body in client.requested:
        assert "limit" not in (params or {})


def test_a_delta_sweep_reports_no_closable_scope():
    """A delta only sees its window, so absence proves nothing and nothing may be closed."""
    from trouveur.sources.workable import WorkableSource

    fresh = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    client = StubClient(
        default=(200, {"jobs": [{"id": "1", "created": fresh, "title": "A"}], "totalSize": 1})
    )
    outcome, seen = asyncio.run(collect(WorkableSource(), client))

    assert len(seen) == 1
    assert outcome.closable_scopes == []


def test_a_delta_sweep_stops_once_postings_fall_outside_the_window():
    """The feed is newest-first, so the first old posting means everything after it is older."""
    from trouveur.sources.workable import WorkableSource

    old = (datetime.now(UTC) - timedelta(days=90)).isoformat().replace("+00:00", "Z")
    new = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    client = StubClient(
        default=(
            200,
            {
                "jobs": [
                    {"id": "new", "created": new, "title": "A"},
                    {"id": "old", "created": old, "title": "B"},
                ],
                "nextPageToken": "more",
                "totalSize": 2,
            },
        )
    )
    _outcome, seen = asyncio.run(collect(WorkableSource(delta_window=timedelta(days=7)), client))

    assert [document.external_id for document in seen] == ["new"]
    # It must stop rather than follow the cursor for ever.
    assert len(client.requested) == 1


def test_a_capped_feed_is_never_closable_even_on_a_backfill():
    """Jobicy serves one capped page; treating it as the corpus would retire everything else."""
    from trouveur.sources.jobicy import JobicySource

    client = StubClient(
        default=(200, {"jobs": [{"id": 1, "jobTitle": "A"}], "jobCount": 1})
    )

    async def run():
        seen = []

        async def sink(documents):
            seen.extend(documents)

        return await JobicySource().sweep(client, sink, backfill=True)

    outcome = asyncio.run(run())
    assert outcome.closable_scopes == []


# --------------------------------------------------------------------------- scope grammar


def test_each_source_validates_scopes_by_its_own_grammar():
    """A Workday board is three facts; the slug rule rejects it outright."""
    from trouveur.sources.registry import clean_scope

    assert clean_scope("greenhouse", "https://job-boards.greenhouse.io/doctolib/") == "doctolib"
    assert (
        clean_scope("workday", "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
        == "nvidia:wd5:NVIDIAExternalCareerSite"
    )
    # Already-canonical scopes pass through, so re-adding a tenant is idempotent.
    assert clean_scope("workday", "nvidia:wd5:NVIDIAExternalCareerSite") == (
        "nvidia:wd5:NVIDIAExternalCareerSite"
    )
    # Case is load-bearing: the API 404s on a lowercased site name.
    assert "NVIDIAExternalCareerSite" in clean_scope(
        "workday", "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    )

    with pytest.raises(SourceError):
        clean_scope("workday", "nvidia")
    with pytest.raises(SourceError):
        clean_scope("greenhouse", "not a slug!")
    # A source with no tenants cannot have one registered.
    with pytest.raises(SourceError):
        clean_scope("workable", "anything")


def test_ashby_boards_may_be_named_after_a_domain():
    """`roadsurfer.com` and `mistral.ai` are live Ashby boards.

    A dot-free slug rule refuses them silently -- they are simply never crawled, and nothing
    reports a board that was never registered. The permission stays Ashby's alone: for every
    other source a dotted "slug" is a pasted company homepage, and rejecting it is the point.
    """
    from trouveur.sources.registry import clean_scope

    assert clean_scope("ashby", "mistral.ai") == "mistral.ai"
    assert clean_scope("ashby", "https://jobs.ashbyhq.com/roadsurfer.com") == "roadsurfer.com"
    assert clean_scope("ashby", "ramp") == "ramp"

    with pytest.raises(SourceError):
        clean_scope("ashby", ".leading-dot")
    # The same string is still a mistake everywhere else.
    with pytest.raises(SourceError):
        clean_scope("greenhouse", "mistral.ai")
