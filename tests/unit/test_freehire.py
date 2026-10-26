"""freehire as a lead source, over hand-written API pages: one lead per board, cap and block."""

from __future__ import annotations

from trouveur.discovery.freehire import board_key, harvest, plan
from trouveur.sources.errors import SourceBlocked


def _job(platform: str, tenant: str, n: int) -> dict:
    return {
        "source": platform,
        "external_id": f"{tenant}:{n}",
        "company_slug": tenant,
        "company": tenant.title(),
        "title": "Prozessingenieur",
        "location": "Wien, at",
        "countries": ["at"],
        "url": f"https://jobs.{platform}.example/{tenant}/{n}",
        "description": "<p>" + "lang " * 500 + "</p>",
    }


class _Stub:
    def __init__(self, pages: dict[str, list[dict]], *, block_after: int | None = None) -> None:
        self.pages = pages
        self.block_after = block_after
        self.asked: list[dict] = []

    async def get_json(self, url, *, params=None, headers=None):
        self.asked.append({"path": url.rsplit("/v1", 1)[1], **(params or {})})
        if self.block_after is not None and len(self.asked) > self.block_after:
            raise SourceBlocked("freehire answered HTTP 429 after every retry.")
        if url.endswith("/jobs/facets"):
            counts = {name: len(rows) for name, rows in self.pages.items()}
            return {"data": {"facets": {"source": counts}, "total": sum(counts.values())}}
        rows = self.pages[params["source"]]
        return {"data": rows[params["offset"] : params["offset"] + params["limit"]],
                "meta": {"total": len(rows)}}


def test_a_board_is_the_platform_and_the_tenant():
    assert board_key(_job("personio", "nahe", 1)) == "personio:nahe"
    no_tenant_in_id = {"source": "join", "external_id": "998", "company_slug": "Acme"}
    assert board_key(no_tenant_in_id) == "join:acme"
    assert board_key({"source": "join", "external_id": "998"}) is None


def test_platforms_we_sweep_go_first_and_whole_aggregators_not_at_all():
    order = plan({"softgarden": 27_000, "dvinci": 40, "personio": 2_000, "arbeitnow": 200})
    assert order == ["personio", "dvinci", "softgarden"]


async def test_many_jobs_of_one_board_make_one_lead_without_their_description():
    stub = _Stub({"personio": [_job("personio", "nahe", n) for n in range(150)]
                  + [_job("personio", "ferne", 1)]})
    found = await harvest(stub, ["AT"], days=7, max_requests=50, known=set())

    assert [lead.payload["board"] for lead in found.leads] == ["personio:nahe", "personio:ferne"]
    assert "description" not in found.leads[0].payload
    assert found.leads[0].payload["external_id"] == "nahe:0"
    assert found.jobs == 151
    assert found.queries_done == found.queries_planned == 1


async def test_a_board_filed_last_week_is_not_filed_again():
    stub = _Stub({"personio": [_job("personio", "nahe", 1), _job("personio", "neu", 1)]})
    found = await harvest(stub, ["AT"], days=7, max_requests=50, known={"personio:nahe"})
    assert [lead.payload["board"] for lead in found.leads] == ["personio:neu"]


async def test_the_request_cap_stops_the_pass():
    stub = _Stub({"personio": [_job("personio", f"b{n}", 1) for n in range(1000)]})
    found = await harvest(stub, ["AT", "DE"], days=7, max_requests=3, known=set())
    assert len(stub.asked) == 3
    assert found.queries_done == 0


async def test_a_block_stops_the_pass_and_keeps_what_it_found():
    stub = _Stub({"personio": [_job("personio", f"b{n}", 1) for n in range(300)]}, block_after=2)
    found = await harvest(stub, ["AT"], days=7, max_requests=50, known=set())
    assert found.blocked
    assert len(found.leads) == 100


async def test_a_filter_the_api_ignored_stops_the_pass():
    class Ignoring(_Stub):
        async def get_json(self, url, *, params=None, headers=None):
            return {"data": [], "meta": {"ignored_params": [{"param": "countries"}]}}

    found = await harvest(Ignoring({}), ["AT"], days=7, max_requests=50, known=set())
    assert found.error and "ignored" in found.error
    assert found.requests == 1
