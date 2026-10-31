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
    # Distinct providers must not be merged into one queue.
    assert throttle_key("https://api.lever.co/v0/x") != throttle_key("https://api.ashbyhq.com/y")


def test_workday_budgets_each_company_on_its_own():
    """Measured: Workday limits each tenant host separately, so one shared budget held 173
    boards to 1 req/s. Its per-company interval is set in the registry against that host."""
    from trouveur.sources.http import throttle_key

    assert throttle_key("https://acme.wd5.myworkdayjobs.com/x") == "acme.wd5.myworkdayjobs.com"
    assert throttle_key("https://acme.wd5.myworkdayjobs.com/x") != throttle_key(
        "https://other.wd5.myworkdayjobs.com/x"
    )


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


def test_workday_pagination_stops_when_the_clamped_page_repeats():
    """Past its last posting Workday clamps the offset and serves the final page forever.

    Probed live on 2026-09-22: offset=2000 and offset=9980 returned the identical row. The empty
    page the walk terminates on therefore never arrives, and without this guard a 2,000-posting
    board costs the full MAX_OFFSET/PAGE_SIZE = 500 requests at 1 req/s shared across every
    tenant. Observed in production as a sweep that ran 8.4 hours and re-fetched one page.
    """
    from trouveur.sources.workday import WorkdaySource

    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
    real = [{"externalPath": "/job/a", "title": "A"}, {"externalPath": "/job/b", "title": "B"}]

    def respond(_params, body):
        # Every offset past the first page returns the last page again, never an empty one.
        page = real[: body["limit"]] if body["offset"] == 0 else real[-1:]
        return (200, {"total": 2, "jobPostings": page})

    client = StubClient({url: respond})
    outcome, seen = asyncio.run(collect(WorkdaySource(boards=["acme:wd5:Careers"]), client))

    assert len(client.requested) == 2, (
        f"clamped page was re-fetched {len(client.requested)} times; the guard did not fire"
    )
    assert [document.external_id for document in seen] == [
        "acme:wd5:Careers:/job/a",
        "acme:wd5:Careers:/job/b",
    ]
    # The clamp is a real end of the board: everything behind it was seen, so it stays closable.
    assert outcome.closable_scopes == ["acme:wd5:Careers"]


def _capped_board(facets, slices):
    """A board whose unfiltered query is capped: page one says 2,000 and, past it, the walk wraps
    to page one again. `slices` maps one applied facet value to that slice's postings."""
    first = [{"externalPath": "/job/a", "title": "A"}]

    def respond(_params, body):
        applied = body["appliedFacets"]
        if not applied:
            return (200, {"total": 2000, "jobPostings": first, "facets": facets})
        (values,) = applied.values()
        rows = slices[values[0]]
        page = rows[body["offset"] : body["offset"] + body["limit"]]
        return (200, {"total": len(rows), "jobPostings": page})

    return respond


def test_workday_capped_board_is_walked_by_facet_and_stays_closable():
    """Some tenants cap a query at 2,000 and then wrap to page one, which reads as the end of
    the board: nine boards lost 50 to 2,100 postings each, and every posting past the cap was
    closed as gone. A posting in two slices (two locations) must be sunk once.

    The split takes a facet summing to the figure two facets agree on, then the cheapest: a
    truncated facet would mark the board complete while missing postings."""
    from trouveur.sources.workday import WorkdaySource

    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
    facets = [
        # A wrapper holds facets, not values; applied as itself it would filter nothing.
        {
            "facetParameter": "locationMainGroup",
            "values": [
                {
                    "facetParameter": "locationCountry",
                    "values": [
                        {"id": "de", "count": 1000},
                        {"id": "fr", "count": 700},
                        {"id": "it", "count": 500},
                    ],
                }
            ],
        },
        # Sums far below the cap, so splitting on it would list a sliver and call it the board.
        {"facetParameter": "distance", "values": [{"id": "near", "count": 3}]},
        # Fewer values, but it sums short of the size the other two agree on.
        {
            "facetParameter": "remoteType",
            "values": [{"id": "onsite", "count": 1100}, {"id": "remote", "count": 1000}],
        },
        # The widest, because it counts a posting once per region it names.
        {
            "facetParameter": "locationRegion",
            "values": [{"id": f"region{n}", "count": 500} for n in range(5)],
        },
        # Agrees on the size, at one request per value.
        {
            "facetParameter": "locations",
            "values": [{"id": f"site{n}", "count": 100} for n in range(22)],
        },
    ]
    slices = {
        "de": [{"externalPath": "/job/a"}, {"externalPath": "/job/b"}],
        "fr": [{"externalPath": "/job/b"}, {"externalPath": "/job/c"}],
        "it": [],
    }
    client = StubClient({url: _capped_board(facets, slices)})
    outcome, seen = asyncio.run(collect(WorkdaySource(boards=["acme:wd5:Careers"]), client))

    assert sorted(document.external_id for document in seen) == [
        "acme:wd5:Careers:/job/a",
        "acme:wd5:Careers:/job/b",
        "acme:wd5:Careers:/job/c",
    ]
    applied = [body["appliedFacets"] for _m, _u, _p, body in client.requested]
    assert {key for facet in applied for key in facet} == {"locationCountry"}
    assert outcome.closable_scopes == ["acme:wd5:Careers"]


def test_workday_capped_board_whose_size_no_two_facets_agree_on_is_walked_but_never_closed():
    """Every facet sum may be wrong -- truncated, or counting a posting once per place -- so
    without two agreeing the walk keeps its postings and closes nothing."""
    from trouveur.sources.workday import WorkdaySource

    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
    facets = [
        {
            "facetParameter": "locationCountry",
            "values": [{"id": "de", "count": 1500}, {"id": "fr", "count": 700}],
        },
        {
            "facetParameter": "remoteType",
            "values": [{"id": "onsite", "count": 1100}, {"id": "remote", "count": 1000}],
        },
    ]
    slices = {"de": [{"externalPath": "/job/a"}], "fr": [{"externalPath": "/job/b"}]}
    client = StubClient({url: _capped_board(facets, slices)})
    outcome, seen = asyncio.run(collect(WorkdaySource(boards=["acme:wd5:Careers"]), client))

    assert sorted(document.external_id for document in seen) == [
        "acme:wd5:Careers:/job/a",
        "acme:wd5:Careers:/job/b",
    ]
    applied = [body["appliedFacets"] for _m, _u, _p, body in client.requested]
    assert {key for facet in applied for key in facet} == {"locationCountry"}
    assert outcome.closable_scopes == []


def test_workday_capped_board_with_no_usable_facet_is_kept_but_never_closed():
    """Unsplittable, the walk still collects what the cap shows, but it cannot have seen the
    rest, so nothing on the board may be closed."""
    from trouveur.sources.workday import WorkdaySource

    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
    facets = [{"facetParameter": "distance", "values": [{"id": "near", "count": 3}]}]
    client = StubClient({url: _capped_board(facets, {})})
    outcome, seen = asyncio.run(collect(WorkdaySource(boards=["acme:wd5:Careers"]), client))

    assert [document.external_id for document in seen] == ["acme:wd5:Careers:/job/a"]
    assert outcome.closable_scopes == []
    assert outcome.partitions_overflowed == 1


def test_workday_page_that_is_not_json_fails_its_board_not_the_source():
    """On 2026-10-23 and 2026-10-31 one 200 with a body that was not JSON raised out of the sweep
    and ended it for all 173 boards, leaving none of them closable."""
    from trouveur.sources.workday import WorkdaySource

    broken = "https://broken.wd5.myworkdayjobs.com/wday/cxs/broken/Careers/jobs"
    fine = "https://fine.wd5.myworkdayjobs.com/wday/cxs/fine/Careers/jobs"
    rows = [{"externalPath": "/job/a"}]
    client = StubClient(
        {
            broken: (200, ""),
            fine: lambda _p, body: (200, {"total": 1, "jobPostings": rows[body["offset"] :]}),
        }
    )
    outcome, seen = asyncio.run(
        collect(WorkdaySource(boards=["broken:wd5:Careers", "fine:wd5:Careers"]), client)
    )

    assert [document.external_id for document in seen] == ["fine:wd5:Careers:/job/a"]
    assert outcome.closable_scopes == ["fine:wd5:Careers"]
    assert outcome.errors and "broken:wd5:Careers" in outcome.errors[0]


def test_workday_challenge_page_served_as_200_is_a_block_not_a_parse_error():
    """`PoliteClient` recognises a challenge only on a refusal status; read as a page that is not
    JSON, a 200 one would fail the board and let the walk go on asking."""
    from trouveur.sources.workday import WorkdaySource

    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
    client = StubClient({url: (200, "<html><title>Just a moment...</title></html>")})
    outcome, seen = asyncio.run(collect(WorkdaySource(boards=["acme:wd5:Careers"]), client))

    assert seen == []
    assert [result.blocked for result in outcome.scope_results] == [True]
    assert outcome.closable_scopes == []


def test_workday_detail_403_s22_is_a_withdrawn_posting_and_any_other_403_is_not():
    """Workday answers a posting taken down after the listing with 403 S22, not 404, so it was
    retried five times and parked, open for ever. Some tenants also answer a burst with a 403,
    and closing on that would retire live postings."""
    from trouveur.sources.workday import WorkdaySource

    external_id = "acme:wd5:Careers:/job/Berlin/Analyst_R1"
    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/job/Berlin/Analyst_R1"
    withdrawn = StubClient(
        {url: (403, {"errorCode": "S22", "httpStatus": 403, "message": "permission denied"})}
    )
    assert asyncio.run(WorkdaySource().fetch_detail(withdrawn, external_id)) is None

    refused = StubClient({url: (403, {"errorCode": "S99", "message": "too many requests"})})
    with pytest.raises(SourceError):
        asyncio.run(WorkdaySource().fetch_detail(refused, external_id))


def test_workday_detail_id_carries_a_three_part_scope():
    """`scoped_id` joins a three-part scope to the path, so the id has four fields, not two.

    Splitting it on the first colon yields the tenant alone and the scope parser then raises a
    bare ValueError -- which `drain_detail` does not catch, so it took down the whole runner tick
    and with it the derive, dedup and embed stages. Workday detail fetching had never once run.
    """
    from trouveur.sources.workday import WorkdaySource

    external_id = "acme:wd5:Careers:/job/Atessa/Manager-Operations_R4482"
    detail_url = (
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers"
        "/job/Atessa/Manager-Operations_R4482"
    )
    client = StubClient({detail_url: (200, {"jobPostingInfo": {"title": "Manager"}})})

    document = asyncio.run(WorkdaySource().fetch_detail(client, external_id))

    assert document is not None, "the detail request did not reach the stubbed URL"
    assert document.external_id == external_id
    assert document.scope == "acme:wd5:Careers"


def test_workday_detail_id_without_a_path_is_refused_as_a_source_error():
    """Must raise SourceError, which the drain catches -- not ValueError, which it does not."""
    from trouveur.sources.workday import WorkdaySource

    with pytest.raises(SourceError):
        asyncio.run(WorkdaySource().fetch_detail(StubClient(), "acme:wd5:Careers"))


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


def test_workday_locations_text_is_a_place_unless_it_is_a_count():
    """`locationsText` reads "3 Locations" on a posting with several places."""
    from trouveur.sources.workday import normalize

    def locations(text, detail=None):
        job = normalize(
            {"title": "Engineer", "locationsText": text},
            detail,
            external_id="acme:wd5:Careers:/job/x",
        )
        assert job is not None
        return [location.raw for location in job.locations]

    assert locations("3 Locations") == []
    assert locations("2 Standorte") == []
    assert locations("Springfield, Massachusetts") == ["Springfield, Massachusetts"]
    detail = {"jobPostingInfo": {"title": "Engineer", "location": "AT, Vienna", "posted": True}}
    assert locations("Vienna", detail) == ["AT, Vienna"]


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


def _arbeitnow_pages(*pages):
    """Arbeitnow links each page to the next by a complete URL, until the last."""
    base = "https://www.arbeitnow.com/api/job-board-api"
    urls = [base] + [f"{base}?page={number}" for number in range(2, len(pages) + 1)]
    responses = {}
    for index, rows in enumerate(pages):
        links = {"next": urls[index + 1]} if index + 1 < len(pages) else {}
        responses[urls[index]] = (200, {"data": rows, "links": links})
    return StubClient(responses=responses)


def test_a_delta_sweep_stops_one_page_past_the_window_and_never_walks_on():
    """The window's edge is a page with nothing inside it. The walk must end there, not follow the
    cursor through years of history, and not stop before the rows of today it has not read."""
    from trouveur.sources.arbeitnow import ArbeitnowSource

    old = int((datetime.now(UTC) - timedelta(days=90)).timestamp())
    new = int(datetime.now(UTC).timestamp())
    client = _arbeitnow_pages(
        [{"slug": "new", "created_at": new, "title": "A"},
         {"slug": "old", "created_at": old, "title": "B"}],
        [{"slug": "older", "created_at": old, "title": "C"}],
        [{"slug": "never-read", "created_at": new, "title": "D"}],
    )
    _outcome, seen = asyncio.run(collect(ArbeitnowSource(delta_window=timedelta(days=7)), client))

    assert [document.external_id for document in seen] == ["new"]
    assert len(client.requested) == 2


def test_a_pinned_old_posting_above_todays_does_not_end_the_sweep():
    """Arbeitnow pins a weeks-old posting at the top of page one. Stopping at the first old row
    collected nothing on 2026-09-29, and the sweep still reported success."""
    from trouveur.sources.arbeitnow import ArbeitnowSource

    pinned = int((datetime.now(UTC) - timedelta(days=48)).timestamp())
    new = int(datetime.now(UTC).timestamp())
    client = _arbeitnow_pages(
        [{"slug": "pinned", "created_at": pinned, "title": "A"},
         {"slug": "today", "created_at": new, "title": "B"}],
        [{"slug": "also-today", "created_at": new, "title": "C"}],
    )
    _outcome, seen = asyncio.run(collect(ArbeitnowSource(delta_window=timedelta(days=7)), client))

    assert [document.external_id for document in seen] == ["today", "also-today"]


def test_workable_asks_for_two_days_not_the_week_it_was_rate_limited_on():
    """Seven days at a fixed 20 a page is ~1,000 requests a night; Workable cut every one of those
    sweeps off with HTTP 429 around page 400."""
    from trouveur.sources.workable import WorkableSource

    client = StubClient(default=(200, {"jobs": [], "totalSize": 0}))
    asyncio.run(collect(WorkableSource(), client))

    assert client.requested[0][2]["day_range"] == "2"


def test_workable_pages_with_pagetoken_not_with_the_name_the_response_uses():
    """The response says `nextPageToken`; the request parameter is `pageToken`.

    Sending the response's own name back is an unknown parameter, and this API ignores unknown
    parameters in silence: every request returns page one. Deduplication then hides it, the
    window is never exhausted, and the walk runs to `feed.MAX_PAGES` -- 20,000 requests at one
    provider, which is the HTTP 429 the sweep reported for nine runs out of ten.
    """
    from trouveur.sources.workable import WorkableSource

    fresh = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    pages = {
        None: {"jobs": [{"id": "1", "created": fresh, "title": "A"}], "nextPageToken": "p2"},
        "p2": {"jobs": [{"id": "2", "created": fresh, "title": "B"}]},
    }
    client = StubClient(default=lambda params, _json: (200, pages[params.get("pageToken")]))
    _outcome, seen = asyncio.run(collect(WorkableSource(), client))

    assert [document.external_id for document in seen] == ["1", "2"]
    for _method, _url, params, _body in client.requested:
        assert "nextPageToken" not in (params or {})


def test_workable_narrows_its_window_in_the_request_and_reads_past_a_boosted_posting():
    """Workable is NOT newest-first: boosted postings lead, ordered by when the boost expires.

    So an advert from four weeks ago sits in front of everything published today, and breaking
    on the first out-of-window row -- which is right for every other feed -- stopped the sweep
    after one page. The window is narrowed server-side with `day_range` instead, and the walk
    runs to the end of what that returns.
    """
    from trouveur.sources.workable import WorkableSource

    stale = (datetime.now(UTC) - timedelta(days=28)).isoformat().replace("+00:00", "Z")
    fresh = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    pages = {
        None: {
            "jobs": [
                {"id": "boosted", "created": stale, "title": "A"},
                {"id": "today", "created": fresh, "title": "B"},
            ],
            "nextPageToken": "p2",
        },
        "p2": {"jobs": [{"id": "also-today", "created": fresh, "title": "C"}]},
    }
    client = StubClient(default=lambda params, _json: (200, pages[params.get("pageToken")]))
    outcome, seen = asyncio.run(
        collect(WorkableSource(delta_window=timedelta(days=7)), client)
    )

    assert [document.external_id for document in seen] == ["boosted", "today", "also-today"]
    assert client.requested[0][2]["day_range"] == "7"
    # Still a delta: a window the server cut is not the corpus.
    assert outcome.closable_scopes == []


def test_a_feed_whose_cursor_stops_advancing_is_abandoned_not_hammered():
    """A page of rows we already hold means the cursor is stuck, whatever the cursor says.

    Without this the walk runs to `feed.MAX_PAGES`, and the first symptom anyone sees is the
    provider's rate limiter rather than the paging bug underneath it.
    """
    from trouveur.sources.workable import WorkableSource

    fresh = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    client = StubClient(
        default=(
            200,
            {"jobs": [{"id": "1", "created": fresh, "title": "A"}], "nextPageToken": "same"},
        )
    )
    outcome, seen = asyncio.run(collect(WorkableSource(), client))

    assert [document.external_id for document in seen] == ["1"]
    assert len(client.requested) == 2
    # A stall is not an end of corpus, so nothing may ever be closed on the strength of it.
    assert outcome.closable_scopes == []


def test_a_capped_feed_is_never_closable_even_on_a_backfill():
    """Jobicy serves one capped page; treating it as the corpus would retire everything else."""
    from trouveur.sources.jobicy import JobicySource

    client = StubClient(default=(200, {"jobs": [{"id": 1, "jobTitle": "A"}], "jobCount": 1}))

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


# ---------- Disabling a source ----------


def test_a_disabled_source_is_not_swept():
    """Retiring a source must not mean deleting its adapter.

    An adapter is months of accumulated knowledge about someone else's API, and the reason to
    stop sweeping one is usually that its postings are not earning the request budget today --
    which can change back.
    """
    from trouveur.sources.registry import build_sources

    enabled = {source.name for source in build_sources(tenants={})}
    assert "arbeitsagentur" in enabled

    remaining = {source.name for source in build_sources(tenants={}, disabled=["arbeitsagentur"])}
    assert "arbeitsagentur" not in remaining
    assert remaining == enabled - {"arbeitsagentur"}


def test_a_source_that_is_not_tenant_scoped_can_still_be_disabled():
    """Arbeitsagentur has no tenant rows, so emptying its crawl set cannot switch it off."""
    from trouveur.sources.registry import SOURCES

    assert SOURCES["arbeitsagentur"].tenant_scoped is False


def test_disabling_a_source_that_does_not_exist_is_refused():
    """A typo would silently sweep a source the operator believes is off."""
    from trouveur.sources.registry import SourceError, build_sources

    with pytest.raises(SourceError, match="arbeitsagentr"):
        build_sources(tenants={}, disabled=["arbeitsagentr"])


def test_an_empty_disabled_setting_disables_nothing():
    """`"".split(",")` is `[""]`, which must not be read as a source named empty string."""
    from trouveur.sources.registry import build_sources

    assert build_sources(tenants={}, disabled="".split(","))
