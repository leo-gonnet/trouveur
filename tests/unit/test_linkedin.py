"""LinkedIn as a recall measure, over hand-written search pages and a stubbed transport: parsing,
the traps that would fake a number, the cap, and a block."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from trouveur.config import Settings, get_settings
from trouveur.discovery import linkedin
from trouveur.discovery.linkedin import END_PAGE, Search, harvest, parse_cards, searches_for
from trouveur.ingest import places
from trouveur.models import UserProfile
from trouveur.sources import http
from trouveur.sources.errors import FetchError


def _card(job_id: int, *, urn: bool = True, tag: str = "div") -> str:
    attrs = f'data-entity-urn="urn:li:jobPosting:{job_id}"' if urn else ""
    return f"""
<li><{tag} class="base-card base-search-card" {attrs}>
  <a class="base-card__full-link"
     href="https://at.linkedin.com/jobs/view/prozessingenieur-{job_id}?refId=x"></a>
  <h3 class="base-search-card__title">  Prozessingenieur (m/w/d) </h3>
  <h4 class="base-search-card__subtitle">
    <a href="https://at.linkedin.com/company/nahe">Nahe GmbH</a></h4>
  <span class="job-search-card__location">Wien, Wien, Österreich</span>
</{tag}></li>"""


def test_a_card_is_read_by_its_urn_or_failing_that_its_link_whatever_its_tag():
    cards = parse_cards(_card(11) + _card(12, urn=False) + _card(13, tag="a") + _card(11))

    assert [card.job_id for card in cards] == ["11", "12", "13"]
    assert (cards[0].title, cards[0].company, cards[0].location) == (
        "Prozessingenieur (m/w/d)", "Nahe GmbH", "Wien, Wien, Österreich"
    )


def test_the_end_page_is_no_results_and_a_page_of_unreadable_markup_is_an_error():
    assert parse_cards(END_PAGE) == []
    assert parse_cards(f"{END_PAGE}\n") == []
    assert parse_cards("  \n") == []
    with pytest.raises(FetchError):
        parse_cards('<li><div class="job-card-v2">Prozessingenieur</div></li>')


def test_towns_are_searched_before_countries_and_a_sentence_is_no_query(monkeypatch):
    vienna = places.Place(id=1, name="Vienna", country="AT", lat=48.2, lon=16.4, population=1)
    monkeypatch.setattr(places, "get", lambda place_id: vienna if place_id == 1 else None)
    queries = ["Prozessingenieur", "Prozessingenieur ", "I want a role " * 10]

    in_town = UserProfile(user_id=1, city_ids=[1], radius_km=40, countries=["AT", "DE"])
    assert searches_for(in_town, queries) == [Search("Prozessingenieur", "Vienna, Austria", 25)]

    countries = UserProfile(user_id=1, countries=["AT", "DE"])
    assert [search.location for search in searches_for(countries, queries)] == [
        "Austria", "Germany"
    ]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(http.asyncio, "sleep", instant)


class _Site:
    """A fake linkedin.com search: ten cards a page, then the end page."""

    def __init__(self, ids: list[int], *, refuse_at: int | None = None, refusal=None) -> None:
        self.ids = ids
        self.refuse_at = refuse_at
        self.refusal = refusal or (lambda request: httpx.Response(999))
        self.asked: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authwall":
            return httpx.Response(200, text="<html>Sign in</html>")
        start = int(parse_qs(urlsplit(str(request.url)).query)["start"][0])
        self.asked.append(start)
        if self.refuse_at is not None and len(self.asked) > self.refuse_at:
            return self.refusal(request)
        page = self.ids[start : start + 10]
        return httpx.Response(200, text="".join(map(_card, page)) if page else END_PAGE)

    def client(self) -> http.PoliteClient:
        client = http.PoliteClient(delay=0)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(self), follow_redirects=True
        )
        return client.paced(0, attempts=1)


_TWO = [Search("Prozessingenieur", "Vienna, Austria", 25), Search("Verfahrenstechnik", "Vienna")]


async def test_a_search_runs_to_its_end_page_and_a_job_seen_before_is_no_new_lead():
    site = _Site(list(range(1, 21)))
    found = await harvest(site.client(), _TWO, max_requests=30, known={"2"})

    assert found.error is None and found.searches_done == 2
    assert site.asked == [0, 10, 20, 0, 10, 20]
    assert len(found.leads) == 19, "a job both searches show is one lead"
    assert "2" not in {lead.payload["linkedin_id"] for lead in found.leads}
    assert not any(lead.url for lead in found.leads)


async def test_the_cap_stops_the_pass_and_keeps_what_it_found():
    site = _Site(list(range(1, 26)))
    found = await harvest(site.client(), _TWO, max_requests=2, known=set())

    assert found.requests == 2 and found.searches_done == 0
    assert len(found.leads) == 20


@pytest.mark.parametrize(
    "refusal",
    [
        lambda request: httpx.Response(999),
        lambda request: httpx.Response(429),
        lambda request: httpx.Response(
            302, headers={"location": "https://www.linkedin.com/authwall"}
        ),
    ],
    ids=["999", "429", "authwall"],
)
async def test_the_first_refusal_stops_the_pass_and_keeps_what_it_found(refusal):
    site = _Site(list(range(1, 16)), refuse_at=1, refusal=refusal)
    found = await harvest(site.client(), _TWO, max_requests=30, known=set())

    assert found.blocked is not None
    assert site.asked == [0, 10], "a refusal is never retried"
    assert len(found.leads) == 10


async def test_every_search_answering_nothing_is_an_error_not_an_empty_market():
    found = await harvest(_Site([]).client(), _TWO, max_requests=30, known=set())

    assert found.searches_done == 2 and found.leads == []
    assert found.error is not None


async def test_switched_off_by_default_asks_nothing(monkeypatch):
    def no_database():
        raise AssertionError("a pass with no budget must not even read the readers")

    monkeypatch.setattr(linkedin, "connect", no_database)
    assert Settings.model_fields["linkedin_max_requests"].default == 0
    off = get_settings().model_copy(update={"linkedin_max_requests": 0})
    assert await linkedin.run(off) is None
