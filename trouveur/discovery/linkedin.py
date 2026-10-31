"""LinkedIn's logged-out job search as the measure of how much of it we already hold. Off until an
operator gives it a daily request budget (`linkedin_max_requests`).

The budget is spent as visits spread over the day, each made by one reader's own guest browser: a
search page, a page or two more, sometimes a second query, then minutes to hours of nothing. The
cards a visit saw are judged after the nightly sweep, against a corpus that had its chance at them.

From JobSpy (speedyapply/JobSpy, `jobspy/linkedin`, MIT) and Scrapfly's scraper, October 2026;
correct with the date when probed from our server:
- `/jobs/search` is the page a visitor loads: its first cards, and their total in
  `.results-context-header__job-count`. `seeMoreJobPostings/search` is the request that page sends
  for more, an HTML fragment of cards from `start`.
- Past the last result the fragment is `END_PAGE`, not an empty body.
- `f_TPR=r86400` is "posted in the past 24 hours", `distance` is in miles, and `location` is text.
- LinkedIn answers HTTP 999 to a client it takes for a bot, and sends a logged-out client it
  wants to stop to `/authwall`.

Measured from our server on 2026-10-29: one request every 6 s, from httpx with our own
User-Agent, drew a 429 on the 32nd.

No job pages: since July 2026 they no longer show a logged-out visitor the apply link, and they
are what LinkedIn throttles first. No login and no account, ever.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.cookiejar import Cookie
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError
from selectolax.parser import HTMLParser

from trouveur import clock, versions
from trouveur.config import Settings, get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import discovery as discovery_q
from trouveur.db.queries import ingest as ingest_q
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.discovery.work import record_leads
from trouveur.ingest import places
from trouveur.ingest.derive import derive
from trouveur.ingest.pipeline import SLOW_DAYS, pause_after, slowdown
from trouveur.match import expand
from trouveur.match.pipeline import profile_from_row
from trouveur.models import COUNTRY_NAMES, CanonicalJob, Lead, LeadOrigin, Location, UserProfile
from trouveur.sources.errors import FetchError, SourceBlocked

log = logging.getLogger(__name__)

SOURCE = "linkedin"
LANDING = "https://www.linkedin.com/jobs"
SEARCH_PAGE = "https://www.linkedin.com/jobs/search"
MORE = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
END_PAGE = "<!DOCTYPE html>\n\n<!---->"
# curl_cffi rather than PoliteClient: httpx's TLS and HTTP/2 handshake give it away whatever
# User-Agent it sends. Pinned, so a jar keeps one browser for its whole life.
IMPERSONATE = "chrome150"
# Local wall-clock hours. The window runs past midnight: some people do search at one in the
# morning, and nobody at five.
WINDOW_OPENS = 7
WINDOW_CLOSES = 2
# The day's budget is aimed at this share of the window, so a day whose visits come out shorter
# than expected spends the rest in the hours after rather than in a rush before it closes.
SPEND_WITHIN = 0.8
# Between two visits of anyone: one IP, so two readers at the same minute is one fast client.
MIN_GAP = timedelta(minutes=4)
# A visit due longer ago than this was missed (runner down, a pause after a block) and is moved,
# never made up: several overdue visits in a row are a burst.
MISSED = timedelta(minutes=30)
READ_MEDIAN_SECONDS = 8.0
READ_SPREAD = 0.6
READ_BOUNDS = (5.0, 25.0)
PAGES = (1, 2, 3)
PAGE_WEIGHTS = (45, 35, 20)
SECOND_QUERY = 0.4
# A returning guest is normal; a whole year on one browser id is not.
JAR_DAYS = (5.0, 9.0)
PER_VISIT_GUESS = 2.0
KEEP_REQUESTS_DAYS = 30
# A query longer than this is a sentence from the profile, which LinkedIn ANDs into nothing.
MAX_QUERY_CHARS = 60
MAX_QUERIES = 6
# A held copy counts if it is within this distance of the town the card names: a suburb on one
# site is the city on another.
NEAR_KM = 30
# A card naming more countries than this ("European Union") does not say where the job is.
MAX_COUNTRIES = 2

# Any tag: a card is not always a div. The urn is on the card; a card without one is read by its
# link instead.
_CARDS = ".base-search-card, [data-entity-urn^='urn:li:jobPosting:']"
_JOB_ID = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d+)")
_WALLS = ("/authwall", "/login", "/checkpoint", "/uas/")
_LANGUAGE = "en-US,en;q=0.9"
_NAVIGATE = {"Accept-Language": _LANGUAGE, "Sec-Fetch-Site": "same-origin"}
# What the search page's own script sends for more cards. None removes a header the impersonated
# browser adds to a page load and never to a script's request.
_SCRIPT = {
    "Accept": "*/*",
    "Accept-Language": _LANGUAGE,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": None,
    "Upgrade-Insecure-Requests": None,
}

_rng = random.Random()


@dataclass(frozen=True)
class Search:
    keywords: str
    location: str
    distance_miles: int | None = None


@dataclass(frozen=True)
class Card:
    job_id: str
    title: str | None = None
    company: str | None = None
    location: str | None = None


@dataclass
class Sent:
    """One request, as `linkedin_request` keeps it. No cookie value, ever."""

    sent_at: datetime
    kind: str
    status: int | None
    elapsed_ms: int
    cards: int = 0
    retry_after: str | None = None
    set_cookies: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Visit:
    searches_planned: int = 0
    searches_done: int = 0
    cards: dict[str, Card] = field(default_factory=dict)
    sent: list[Sent] = field(default_factory=list)
    blocked: str | None = None
    error: str | None = None

    @property
    def requests(self) -> int:
        return len(self.sent)


@dataclass(frozen=True)
class Answer:
    status: int
    location: str
    text: str
    retry_after: str | None
    set_cookies: list[str]


class Browser:
    """One reader's guest browser: Chrome's handshake and headers, and that reader's cookie jar.
    Redirects are not followed, so a wall is seen rather than fetched."""

    def __init__(self, cookies: list[dict]) -> None:
        self._session = AsyncSession(impersonate=IMPERSONATE, timeout=30)
        for saved in cookies:
            # Through the jar itself: `Cookies.set` drops the expiry, so a cookie LinkedIn meant
            # to lapse would outlive it in every jar we reload.
            cookie = Cookie(
                0, saved["name"], saved["value"], None, False, saved["domain"], True,
                saved["domain"].startswith("."), saved["path"], True, saved.get("secure", True),
                saved["expires"], saved["expires"] is None, None, None, {},
            )
            if not cookie.is_expired():
                self._session.cookies.jar.set_cookie(cookie)

    async def __aenter__(self) -> Browser:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._session.close()

    async def get(
        self, url: str, *, params: dict, headers: Mapping[str, str | None]
    ) -> Answer:
        try:
            response = await self._session.get(
                url, params=params, headers=dict(headers), allow_redirects=False
            )
        except RequestsError as exc:
            raise FetchError(f"LinkedIn could not be reached at {url}: {exc}") from exc
        return Answer(
            status=response.status_code,
            location=response.headers.get("location") or "",
            text=response.text,
            retry_after=response.headers.get("retry-after"),
            set_cookies=sorted(response.cookies.keys()),
        )

    def cookies(self) -> list[dict]:
        return [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
             "secure": c.secure, "expires": c.expires}
            for c in self._session.cookies.jar
            if not c.is_expired()
        ]


def miles(radius_km: int) -> int:
    return max(5, min(100, round(radius_km / 1.609)))


def window(now: datetime) -> tuple[datetime, datetime]:
    """The visiting window `now` falls in, else the next one, as UTC instants.

    On the local wall clock, like the nightly slot, so seven in the morning stays seven when the
    clocks change.
    """
    local = clock.to_local(now)
    opens = local.replace(hour=WINDOW_OPENS, minute=0, second=0, microsecond=0)
    if local.hour < WINDOW_CLOSES:
        opens -= timedelta(days=1)
    closes = (opens + timedelta(days=1)).replace(hour=WINDOW_CLOSES)
    return opens.astimezone(UTC), closes.astimezone(UTC)


def next_visit(
    now: datetime, *, share: int, used: int, per_visit: float, rng: random.Random
) -> datetime:
    """When a reader visits next: a random gap sized so the visits left in their share of the
    budget fit in what is left of the window, or a random start in the next one once spent."""
    opens, closes = window(now)
    if used >= share:
        opens, closes = window(closes)
        used = 0
    begin = max(now, opens)
    aim = opens + (closes - opens) * SPEND_WITHIN
    visits = max((share - used) / per_visit, 1.0)
    if begin < aim:
        gap = timedelta(seconds=rng.expovariate((visits + 1) / (aim - begin).total_seconds()))
        return min(begin + max(gap, MIN_GAP), aim + (closes - aim) / 2)
    # Past the aim, what is left must still fit before the window closes.
    room = (closes - begin) / (visits + 1)
    gap = timedelta(seconds=rng.expovariate(1 / room.total_seconds()))
    return begin + min(max(gap, MIN_GAP), room)


def read_seconds(rng: random.Random) -> float:
    """Time on a page before the next: mostly short, sometimes long."""
    low, high = READ_BOUNDS
    return min(max(rng.lognormvariate(math.log(READ_MEDIAN_SECONDS), READ_SPREAD), low), high)


def jar_life(rng: random.Random) -> timedelta:
    return timedelta(days=rng.uniform(*JAR_DAYS))


def searches_for(profile: UserProfile, queries: list[str]) -> list[Search]:
    """One search per query and town, or per query and country when the reader picked no town."""
    kept = [
        query
        for query in dict.fromkeys(query.strip() for query in queries)
        if query and len(query) <= MAX_QUERY_CHARS
    ][:MAX_QUERIES]
    towns = [place for place in map(places.get, profile.city_ids) if place is not None]
    if towns:
        where = [
            (f"{town.name}, {COUNTRY_NAMES.get(town.country.upper(), town.country)}",
             miles(profile.radius_km))
            for town in towns
        ]
    else:
        where = [(COUNTRY_NAMES.get(code.upper(), code), None) for code in profile.countries]
    return [Search(query, location, distance) for location, distance in where for query in kept]


def _text(node, selector: str) -> str | None:
    found = node.css_first(selector)
    text = found.text(strip=True) if found is not None else ""
    return " ".join(text.split()) or None


def _card(node) -> Card | None:
    urn = node.attributes.get("data-entity-urn") or ""
    job_id = urn.rsplit(":", 1)[-1] if urn.startswith("urn:li:jobPosting:") else ""
    link = node.css_first("a.base-card__full-link") or node.css_first("a[href*='/jobs/view/']")
    if not job_id.isdigit() and link is not None:
        match = _JOB_ID.search(link.attributes.get("href") or "")
        job_id = match.group(1) if match else ""
    if not job_id.isdigit():
        return None
    return Card(
        job_id=job_id,
        title=_text(node, ".base-search-card__title"),
        company=_text(node, ".base-search-card__subtitle"),
        location=_text(node, ".job-search-card__location"),
    )


def parse_cards(html: str) -> list[Card]:
    """The job cards of one search page; none past the last result.

    Any other page without a card we can read is an error, never "no results": a block or a
    markup change would otherwise read as an empty market, and the day's number as complete.
    """
    if html.strip() in {"", END_PAGE}:
        return []
    cards = {card.job_id: card for card in map(_card, HTMLParser(html).css(_CARDS)) if card}
    if not cards:
        raise FetchError("LinkedIn answered a search page with no job card we can read in it.")
    return list(cards.values())


def parse_search_page(html: str) -> tuple[list[Card], int | None]:
    """The search page's cards and the total it names. None when it names none we can read, or
    only a lower bound ("1,000+").

    No card and no count of zero is an error, never "no results": a block or a markup change
    would otherwise read as an empty market.
    """
    tree = HTMLParser(html)
    node = tree.css_first(".results-context-header__job-count")
    text = node.text(strip=True) if node is not None else ""
    digits = re.sub(r"\D", "", text)
    total = int(digits) if digits and "+" not in text else None
    cards = {card.job_id: card for card in map(_card, tree.css(_CARDS)) if card}
    if not cards and total != 0:
        raise FetchError("LinkedIn's search page held no job card we can read, and no zero count.")
    return list(cards.values()), total


async def _fetch(
    browser: Browser, found: Visit, kind: str, url: str, params: dict,
    headers: Mapping[str, str | None],
) -> tuple[str, Sent]:
    """One request, logged whatever it answered. Raises SourceBlocked at the first refusal."""
    sent_at, started = datetime.now(UTC), time.monotonic()
    try:
        answer = await browser.get(url, params=params, headers=headers)
    except FetchError as exc:
        found.sent.append(
            Sent(sent_at, kind, None, round((time.monotonic() - started) * 1000), error=str(exc))
        )
        raise
    sent = Sent(
        sent_at, kind, answer.status, round((time.monotonic() - started) * 1000),
        retry_after=answer.retry_after, set_cookies=answer.set_cookies,
    )
    found.sent.append(sent)
    if answer.status in {403, 429, 999} or any(wall in answer.location for wall in _WALLS):
        sent.error = f"refused with HTTP {answer.status}"
        raise SourceBlocked(
            f"LinkedIn refused {url} with HTTP {answer.status}"
            + (f", sending us to {answer.location}." if answer.location else ".")
        )
    if answer.status != 200:
        sent.error = f"HTTP {answer.status}"
        raise FetchError(
            f"LinkedIn answered {url} with HTTP {answer.status}"
            + (f", redirecting to {answer.location}." if answer.location else ".")
        )
    return answer.text, sent


async def _browse(
    browser: Browser, found: Visit, search: Search, *, referer: str, pages: int,
    allowance: int, rng: random.Random,
) -> str:
    """One search as a person makes it: the page, then more cards while they scroll. Returns the
    page's address, which is the referer of whatever comes next."""
    params = {"keywords": search.keywords, "location": search.location, "f_TPR": "r86400"}
    if search.distance_miles:
        params["distance"] = search.distance_miles
    page = {**params, "position": 1, "pageNum": 0}
    text, sent = await _fetch(
        browser, found, "search", SEARCH_PAGE, page, {**_NAVIGATE, "Referer": referer}
    )
    cards, total = parse_search_page(text)
    address = f"{SEARCH_PAGE}?{urlencode(page)}"
    seen = sent.cards = len(cards)
    found.cards.update((card.job_id, card) for card in cards)
    for _ in range(pages - 1):
        if not cards or (total is not None and seen >= total) or found.requests >= allowance:
            break
        await asyncio.sleep(read_seconds(rng))
        text, sent = await _fetch(
            browser, found, "more", MORE, {**params, "start": seen},
            {**_SCRIPT, "Referer": address},
        )
        cards = parse_cards(text)
        sent.cards = len(cards)
        seen += len(cards)
        found.cards.update((card.job_id, card) for card in cards)
    return address


async def visit(
    browser: Browser, searches: list[Search], *, first: int, allowance: int, rng: random.Random
) -> Visit:
    """One visit: the reader's next search, and sometimes the one after it. Stops at the
    allowance or the first refusal, keeping what it found."""
    count = 2 if len(searches) > 1 and rng.random() < SECOND_QUERY else 1
    found = Visit(searches_planned=count)
    referer = LANDING
    try:
        for offset in range(count):
            if found.requests >= allowance:
                break
            if offset:
                await asyncio.sleep(read_seconds(rng))
            referer = await _browse(
                browser, found, searches[(first + offset) % len(searches)], referer=referer,
                pages=rng.choices(PAGES, PAGE_WEIGHTS)[0], allowance=allowance, rng=rng,
            )
            found.searches_done += 1
    except SourceBlocked as exc:
        found.blocked = str(exc)
        log.warning("LinkedIn blocked us, stopping: %s", exc)
    except FetchError as exc:
        found.error = str(exc)
        log.warning("LinkedIn visit stopped: %s", exc)
    return found


def lead_for(card: Card) -> Lead:
    """No URL: LinkedIn is not a board, so its link would only count as a host we cannot read."""
    return Lead(
        origin=LeadOrigin.LINKEDIN,
        company=card.company,
        title=card.title,
        location_text=card.location,
        payload={"linkedin_id": card.job_id},
    )


def where(lead: Lead) -> tuple[list[int], list[str]] | None:
    """Where a held copy may be: the towns near the one the card names, else the countries it
    names. None when it names neither, or a region too wide for a match to prove anything."""
    if not lead.location_text:
        return None
    facets = derive(
        CanonicalJob(
            source=SOURCE, external_id=lead.payload["linkedin_id"], url="",
            title=lead.title or "", locations=[Location(raw=lead.location_text)],
        )
    )
    if facets.place_ids:
        return places.area(facets.place_ids, NEAR_KM)[0], []
    countries = facets.countries or facets.unplaced_countries
    return ([], countries) if 0 < len(countries) <= MAX_COUNTRIES else None


async def judge(conn, leads: list[Lead]) -> list[Lead]:
    """Mark each lead with our open copy of its job, by company, title and place, before it is
    recorded.

    Strict on purpose: a false miss lowers the number, a false hit would hide a gap -- a big
    employer posts one title in many towns, and our Munich copy is not the Vienna job. A lead
    missing a company, a title or a readable place is not judged, so it cannot count as a miss.
    """
    wanted = {}
    for key, lead in enumerate(leads):
        place = where(lead) if lead.company and lead.title else None
        if place is not None:
            wanted[key] = (lead.company, lead.title, *place)
    held = await discovery_q.held_jobs(conn, wanted)
    return [
        lead.model_copy(
            update={"held": key in held if key in wanted else None, "held_job_id": held.get(key)}
        )
        for key, lead in enumerate(leads)
    ]


async def reader_searches(conn) -> dict[int, list[Search]]:
    """Each active reader's searches: their cached expanded queries, else the deterministic ones.

    Never an LLM call: an expansion is paid for by the match, and this only reuses it.
    """
    planned: dict[int, list[Search]] = {}
    for user in await users_q.list_users(conn):
        row = await users_q.get_profile(conn, user.id) if user.is_active else None
        if row is None:
            continue
        profile = profile_from_row(row)
        cached = await match_q.get_query_expansion(
            conn, user.id, profile.version, versions.QUERY_EXPANSION_VERSION
        )
        queries = cached.queries if cached else expand.deterministic_queries(profile)
        if searches := searches_for(profile, queries):
            planned[user.id] = searches
    return planned


async def visit_due(
    settings: Settings | None = None,
    *,
    force: bool = False,
    browser: Callable[[list[dict]], Browser] = Browser,
) -> Visit | None:
    """The visit of the reader due first, if any is. None when switched off, paused after a
    block, out of budget, or when nobody is due.

    The day's budget is shared evenly between readers, and divided by the slowdown a recent block
    earned, as a sweep's pace is. `force` skips the visit clock and the window, never the budget,
    the pause or the gap between visits.
    """
    settings = settings or get_settings()
    if settings.linkedin_max_requests <= 0:
        return None
    now = datetime.now(UTC)
    async with connect() as conn:
        history = (await ingest_q.block_history(conn, days=SLOW_DAYS)).get(SOURCE)
        if history and history.streak and now < history.last_blocked_at + pause_after(
            history.streak
        ):
            return None
        budget = settings.linkedin_max_requests // slowdown(history.recent if history else 0)
        searches = await reader_searches(conn)
        share = budget // len(searches) if searches else 0
        if share <= 0:
            return None
        opens, closes = window(now)
        used = await discovery_q.linkedin_requests_since(conn, opens)
        last = await discovery_q.linkedin_last_request_at(conn)
        per_visit = await discovery_q.linkedin_requests_per_visit(
            conn, now - timedelta(days=SLOW_DAYS)
        ) or PER_VISIT_GUESS
        visitors = await discovery_q.linkedin_visitors(conn)
        for user_id in searches:
            row = visitors.get(user_id)
            # A new reader gets a clock rather than a visit, and so does one who missed theirs.
            if row is None or now - row.next_visit_at > MISSED:
                await discovery_q.save_linkedin_visitor(
                    conn,
                    user_id=user_id,
                    cookies=row.cookies if row else [],
                    jar_expires_at=row.jar_expires_at if row else now + jar_life(_rng),
                    next_visit_at=next_visit(
                        now, share=share, used=used.get(user_id, 0), per_visit=per_visit,
                        rng=_rng,
                    ),
                    next_search=row.next_search if row else 0,
                )
        visitors = await discovery_q.linkedin_visitors(conn)
        due = sorted(
            (
                row for user_id, row in visitors.items()
                if user_id in searches and used.get(user_id, 0) < share
                and (force or (opens <= now and row.next_visit_at <= now))
            ),
            key=lambda row: row.next_visit_at,
        )
        if (
            not due
            or sum(used.values()) >= budget
            or (last is not None and now - last < MIN_GAP)
        ):
            return None
        reader = due[0]
        sweep_id, _ = await ingest_q.start_sweep(conn, SOURCE)

    spent = used.get(reader.user_id, 0)
    fresh = reader.jar_expires_at <= now
    async with browser([] if fresh else list(reader.cookies)) as guest:
        found = await visit(
            guest, searches[reader.user_id], first=reader.next_search,
            allowance=min(share - spent, budget - sum(used.values())), rng=_rng,
        )
        jar = guest.cookies()
    done = datetime.now(UTC)

    async with connect() as conn:
        await discovery_q.record_linkedin_requests(
            conn,
            [
                {"sweep_id": sweep_id, "user_id": reader.user_id, **vars(sent)}
                for sent in found.sent
            ],
            keep_days=KEEP_REQUESTS_DAYS,
        )
        await discovery_q.add_linkedin_seen(
            conn,
            [
                {"linkedin_id": card.job_id, "company": card.company, "title": card.title,
                 "location_text": card.location}
                for card in found.cards.values()
            ],
        )
        await discovery_q.save_linkedin_visitor(
            conn,
            user_id=reader.user_id,
            cookies=jar,
            jar_expires_at=done + jar_life(_rng) if fresh else reader.jar_expires_at,
            next_visit_at=next_visit(
                done, share=share, used=spent + found.requests, per_visit=per_visit, rng=_rng
            ),
            next_search=reader.next_search + found.searches_done,
        )
        await ingest_q.finish_sweep(
            conn,
            sweep_id,
            ok=not (found.blocked or found.error),
            complete=False,
            partitions_total=found.searches_planned,
            partitions_done=found.searches_done,
            partitions_overflowed=0,
            documents_seen=len(found.cards),
            jobs_upserted=0,
            jobs_closed=0,
            error=found.blocked or found.error,
            blocked=found.blocked is not None,
        )
    log.info(
        "linkedin: reader %d visited, %d request(s), %d card(s)",
        reader.user_id, found.requests, len(found.cards),
    )
    return found


async def record_seen() -> list[Lead]:
    """Judge and record what the visits saw, once the nightly sweep has run. Free: no request."""
    async with connect() as conn:
        rows = await discovery_q.linkedin_seen_cards(conn)
        known = await discovery_q.payload_values(conn, origin=SOURCE, key="linkedin_id")
        leads = await judge(
            conn,
            [
                lead_for(Card(row.linkedin_id, row.title, row.company, row.location_text))
                for row in rows
                if row.linkedin_id not in known
            ],
        )
    written = await record_leads(leads)
    # After the write: a crash before it leaves the cards to judge tomorrow, never lost.
    async with connect() as conn:
        await discovery_q.forget_linkedin_seen(conn, [row.linkedin_id for row in rows])
    log.info(
        "linkedin: %d card(s) seen, %d new lead(s), %d already held",
        len(rows), written, sum(bool(lead.held) for lead in leads),
    )
    return leads
