"""LinkedIn as a recall measure, over hand-written search pages and a stubbed browser: parsing, the
traps that would fake a number, the day's schedule, one visit's shape, and a block."""

from __future__ import annotations

import random
import statistics
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trouveur.config import Settings, get_settings
from trouveur.discovery import linkedin
from trouveur.discovery.linkedin import (
    END_PAGE,
    LANDING,
    MIN_GAP,
    SEARCH_PAGE,
    Answer,
    Browser,
    Search,
    next_visit,
    parse_cards,
    parse_search_page,
    read_seconds,
    searches_for,
    visit,
    where,
    window,
)
from trouveur.ingest import places
from trouveur.models import Lead, LeadOrigin, UserProfile
from trouveur.sources.errors import FetchError

VIENNA = ZoneInfo("Europe/Vienna")


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


def _search_page(ids: list[int], count: str | None) -> str:
    header = (
        f'<span class="results-context-header__job-count">{count}</span>' if count else ""
    )
    return f"<html><body>{header}<ul>{''.join(map(_card, ids))}</ul></body></html>"


def test_the_search_page_names_its_total_unless_it_names_only_a_lower_bound():
    cards, total = parse_search_page(_search_page([1, 2], "1,234"))
    assert [card.job_id for card in cards] == ["1", "2"] and total == 1234
    assert parse_search_page(_search_page([1], "1,000+"))[1] is None
    assert parse_search_page(_search_page([1], None))[1] is None


def test_a_search_page_without_cards_is_empty_only_when_it_says_zero():
    assert parse_search_page(_search_page([], "0")) == ([], 0)
    with pytest.raises(FetchError):
        parse_search_page("<html><body>Something went wrong</body></html>")


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


def _lead(place: str | None) -> Lead:
    return Lead(origin=LeadOrigin.LINKEDIN, company="Nahe", title="Prozessingenieur",
                location_text=place, payload={"linkedin_id": "1"})


def test_a_held_copy_must_be_near_the_town_the_card_names_or_in_its_country():
    vienna = where(_lead("Wien, Wien, Österreich"))
    assert vienna is not None and vienna[1] == []
    near = set(vienna[0])
    assert {place.id for place in places.named("Schwechat")} & near, "a suburb is the city"
    assert not {place.id for place in places.named("München")} & near

    assert where(_lead("Austria")) == ([], ["AT"])
    assert where(_lead("European Union")) is None, "a wide region says nowhere"
    assert where(_lead(None)) is None


@pytest.fixture
def vienna(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Vienna")


def _at(*args) -> datetime:
    return datetime(*args, tzinfo=VIENNA).astimezone(UTC)


def test_the_window_runs_past_midnight_on_the_local_clock_whatever_the_season(vienna):
    opens, closes = window(_at(2026, 1, 15, 1, 30))
    assert (opens, closes) == (_at(2026, 1, 14, 7), _at(2026, 1, 15, 2)), "1:30 is yesterday's"
    assert window(_at(2026, 1, 15, 4)) == (_at(2026, 1, 15, 7), _at(2026, 1, 16, 2)), (
        "at four the next window is this morning's"
    )
    summer = window(_at(2026, 7, 15, 12))[0]
    assert summer.astimezone(VIENNA).hour == 7 and summer.hour == 5


def test_a_day_of_visits_spends_the_share_inside_the_window_never_more_and_mostly_early(vienna):
    """The schedule's promise: the day's share is reached and never passed, in visits minutes to
    hours apart, and nearly always hours before the window closes."""
    opens, closes = window(_at(2026, 3, 10, 12))
    early = 0
    for seed in range(50):
        rng = random.Random(seed)
        when = next_visit(opens - timedelta(hours=3), share=30, used=0, per_visit=2, rng=rng)
        used, visits = 0, []
        while when < closes:
            assert opens <= when, "no visit at night"
            visits.append(when)
            used += min(2, 30 - used)
            when = next_visit(when, share=30, used=used, per_visit=2, rng=rng)
        assert used == 30
        assert when >= window(closes)[0], "a spent share waits for the next window"
        assert all(b - a >= MIN_GAP for a, b in zip(visits, visits[1:], strict=False))
        early += visits[-1] < closes - timedelta(hours=2)
    assert early >= 40, "the share is spent with hours to spare"


def test_reading_time_is_mostly_short_with_a_long_tail():
    rng = random.Random(7)
    waits = [read_seconds(rng) for _ in range(2000)]
    assert 5 <= min(waits) and max(waits) <= 25
    assert statistics.median(waits) < 10
    assert sum(wait > 15 for wait in waits) > 40, "never a long read is a machine's rhythm"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(linkedin.asyncio, "sleep", instant)


class _Guest:
    """A fake guest browser over a fake linkedin.com: a search page of `first` cards with their
    total, then ten a fragment, then the end page."""

    def __init__(self, ids: list[int], *, first: int = 25, count: bool = True,
                 refuse_at: int | None = None, refusal: Answer | None = None) -> None:
        self.ids, self.first, self.count = ids, first, count
        self.refuse_at = refuse_at
        self.refusal = refusal or Answer(999, "", "", None, [])
        self.asked: list[tuple[str, dict, dict]] = []

    async def get(self, url, *, params, headers):
        self.asked.append((url, params, dict(headers)))
        if self.refuse_at is not None and len(self.asked) > self.refuse_at:
            return self.refusal
        if url == SEARCH_PAGE:
            count = f"{len(self.ids)}" if self.count else None
            return Answer(200, "", _search_page(self.ids[: self.first], count), None, ["lidc"])
        page = self.ids[params["start"] : params["start"] + 10]
        return Answer(200, "", "".join(map(_card, page)) if page else END_PAGE, None, [])


class _Rng(random.Random):
    """Every visit pages as deep as it may, and asks a second query."""

    def random(self):
        return 0.0

    def choices(self, population, weights=None, **kwargs):
        return [max(population)]

    def lognormvariate(self, mu, sigma):
        return 8.0


_TWO = [Search("Prozessingenieur", "Vienna, Austria", 25), Search("Verfahrenstechnik", "Vienna")]


async def test_a_visit_loads_the_search_page_then_asks_for_more_as_the_page_itself_would():
    guest = _Guest(list(range(1, 60)), count=False)
    found = await visit(guest, _TWO[:1], first=0, allowance=10, rng=_Rng())

    (page_url, page, page_headers), (more_url, more, more_headers), _ = guest.asked
    assert (page_url, page_headers["Referer"]) == (SEARCH_PAGE, LANDING)
    assert more_url != SEARCH_PAGE and more["start"] == 25, "more starts after what was shown"
    assert more_headers["Referer"].startswith(SEARCH_PAGE + "?keywords=Prozessingenieur")
    assert more_headers["Sec-Fetch-Mode"] == "cors"
    assert more_headers["Upgrade-Insecure-Requests"] is None, "no page-load header on a script's"
    assert found.requests == 3 and len(found.cards) == 45
    assert [(sent.kind, sent.status, sent.cards) for sent in found.sent] == [
        ("search", 200, 25), ("more", 200, 10), ("more", 200, 10)
    ]
    assert found.sent[0].set_cookies == ["lidc"]


async def test_a_total_the_page_names_spares_the_request_for_the_end_page():
    guest = _Guest(list(range(1, 31)))
    found = await visit(guest, _TWO[:1], first=0, allowance=10, rng=_Rng())

    assert [params["start"] for _, params, _ in guest.asked[1:]] == [25]
    assert found.requests == 2 and len(found.cards) == 30


async def test_a_second_query_comes_from_the_first_ones_page_and_the_allowance_stops_both():
    guest = _Guest(list(range(1, 6)))
    found = await visit(guest, _TWO, first=1, allowance=10, rng=_Rng())

    first, second = guest.asked
    assert first[1]["keywords"] == "Verfahrenstechnik", "the visit starts at the reader's next"
    assert second[1]["keywords"] == "Prozessingenieur"
    assert second[2]["Referer"].startswith(SEARCH_PAGE + "?keywords=Verfahrenstechnik")
    assert found.searches_done == 2

    capped = await visit(_Guest(list(range(1, 60))), _TWO, first=0, allowance=2, rng=_Rng())
    assert capped.requests == 2 and capped.searches_done == 1


@pytest.mark.parametrize(
    "refusal",
    [
        Answer(999, "", "", None, []),
        Answer(429, "", "", "3600", []),
        Answer(302, "https://www.linkedin.com/authwall?trk=x", "", None, []),
    ],
    ids=["999", "429", "authwall"],
)
async def test_the_first_refusal_stops_the_visit_unretried_and_keeps_what_it_found(refusal):
    guest = _Guest(list(range(1, 60)), refuse_at=1, refusal=refusal)
    found = await visit(guest, _TWO, first=0, allowance=10, rng=_Rng())

    assert found.blocked is not None and found.error is None
    assert len(guest.asked) == 2, "a refusal is never retried"
    assert len(found.cards) == 25
    assert found.sent[-1].status == refusal.status
    assert found.sent[-1].retry_after == refusal.retry_after


async def test_a_redirect_is_never_followed_and_only_a_wall_reads_as_a_block():
    moved = Answer(301, "https://at.linkedin.com/jobs/search", "", None, [])
    found = await visit(_Guest([1], refuse_at=0, refusal=moved), _TWO, first=0, allowance=10,
                        rng=_Rng())

    assert found.blocked is None and "at.linkedin.com" in found.error


def test_a_jar_keeps_its_cookies_from_one_visit_to_the_next_and_drops_expired_ones():
    kept = {"name": "bcookie", "value": "v=2&abc", "domain": ".linkedin.com", "path": "/",
            "secure": True, "expires": int(time.time()) + 86400}
    stale = {**kept, "name": "lidc", "expires": int(time.time()) - 60}
    browser = Browser([kept, stale])

    assert browser.cookies() == [kept]


async def test_switched_off_by_default_asks_nothing(monkeypatch):
    def no_database():
        raise AssertionError("a pass with no budget must not even read the readers")

    monkeypatch.setattr(linkedin, "connect", no_database)
    assert Settings.model_fields["linkedin_max_requests"].default == 0
    off = get_settings().model_copy(update={"linkedin_max_requests": 0})
    assert await linkedin.visit_due(off) is None
