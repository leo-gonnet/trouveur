"""LinkedIn's logged-out job search as the measure of how much of it we already hold. Daily at
most, search pages only, and off until an operator gives it a request budget
(`linkedin_max_requests`).

NOT PROBED from our server. The facts below come from JobSpy (speedyapply/JobSpy,
`jobspy/linkedin`, MIT, October 2026); probe by hand from the server before setting a budget, and
correct this docstring with the date:
- `seeMoreJobPostings/search` answers an HTML fragment of up to 10 job cards per `start`.
  `f_TPR=r86400` is "posted in the past 24 hours", `distance` is in miles, and `location` is text.
- Past the last result it answers `END_PAGE`, not an empty body.
- LinkedIn answers HTTP 999 to a client it takes for a bot, and sends a logged-out client it
  wants to stop to `/authwall`.

No job pages: since July 2026 they no longer show a logged-out visitor the apply link, and they
are what LinkedIn throttles first. No login and no account, ever.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from selectolax.parser import HTMLParser

from trouveur import versions
from trouveur.config import Settings, get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import discovery as discovery_q
from trouveur.db.queries import ingest as ingest_q
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.discovery.work import record_leads
from trouveur.ingest import places
from trouveur.match import expand
from trouveur.match.pipeline import profile_from_row
from trouveur.models import COUNTRY_NAMES, Lead, LeadOrigin, UserProfile
from trouveur.sources.errors import FetchError, SourceBlocked
from trouveur.sources.http import PoliteClient

log = logging.getLogger(__name__)

SOURCE = "linkedin"
SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
END_PAGE = "<!DOCTYPE html>\n\n<!---->"
PAGE = 10
PAGES_PER_SEARCH = 3
# Slower than JobSpy's average on purpose, and one attempt per request: retrying a 429 is how a
# slowdown becomes a block.
DELAY_SECONDS = 6.0
EVERY = timedelta(hours=20)
# A query longer than this is a sentence from the profile, which LinkedIn ANDs into nothing.
MAX_QUERY_CHARS = 60
MAX_QUERIES = 6

# Any tag: a card is not always a div. The urn is on the card; a card without one is read by its
# link instead.
_CARDS = ".base-search-card, [data-entity-urn^='urn:li:jobPosting:']"
_JOB_ID = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d+)")
_WALLS = ("/authwall", "/login", "/checkpoint", "/uas/")


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
class Harvest:
    requests: int = 0
    searches_planned: int = 0
    searches_done: int = 0
    cards: int = 0
    leads: list[Lead] = field(default_factory=list)
    blocked: str | None = None
    error: str | None = None


def miles(radius_km: int) -> int:
    return max(5, min(100, round(radius_km / 1.609)))


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


async def _page(client: PoliteClient, url: str, params: dict) -> str:
    """One page's HTML. Raises SourceBlocked at the first refusal."""
    response = await client.get(url, params=params)
    final = str(response.url)
    if response.status_code in {403, 429, 999} or any(wall in final for wall in _WALLS):
        raise SourceBlocked(
            f"LinkedIn refused {url} with HTTP {response.status_code} (landed on {final})."
        )
    if response.status_code != 200:
        raise FetchError(f"LinkedIn answered {url} with HTTP {response.status_code}.")
    return response.text


def lead_for(card: Card) -> Lead:
    """No URL: LinkedIn is not a board, so its link would only count as a host we cannot read."""
    return Lead(
        origin=LeadOrigin.LINKEDIN,
        company=card.company,
        title=card.title,
        location_text=card.location,
        payload={"linkedin_id": card.job_id},
    )


async def harvest(
    client: PoliteClient,
    searches: list[Search],
    *,
    max_requests: int,
    known: set[str],
) -> Harvest:
    """Every search's pages, one lead per job not seen before. Stops at the cap or the first
    block, keeping what it found."""
    found = Harvest(searches_planned=len(searches))
    new: dict[str, Card] = {}

    async def page_through(search: Search) -> bool:
        """Every page of one search. False when the budget stopped it."""
        start = 0
        for _ in range(PAGES_PER_SEARCH):
            if found.requests >= max_requests:
                return False
            params = {"keywords": search.keywords, "location": search.location,
                      "f_TPR": "r86400", "start": start}
            if search.distance_miles:
                params["distance"] = search.distance_miles
            found.requests += 1
            cards = parse_cards(await _page(client, SEARCH, params))
            found.cards += len(cards)
            for card in cards:
                if card.job_id not in known:
                    new.setdefault(card.job_id, card)
            if len(cards) < PAGE:
                break
            start += len(cards)
        return True

    try:
        for search in searches:
            if not await page_through(search):
                break
            found.searches_done += 1
    except SourceBlocked as exc:
        found.blocked = str(exc)
        log.warning("LinkedIn blocked us, stopping: %s", exc)
    except FetchError as exc:
        found.error = str(exc)
        log.warning("LinkedIn pass stopped: %s", exc)
    # Every search answering "nothing" is what a quiet block looks like; a day with no number
    # must not pass for a day with nothing to count.
    if found.searches_done and not found.cards and not (found.blocked or found.error):
        found.error = "LinkedIn returned no job card for any search."
    found.leads = [lead_for(card) for card in new.values()]
    return found


async def judge(conn, leads: list[Lead]) -> list[Lead]:
    """Mark each lead with our open copy of its job, by company and title, before it is recorded.

    Strict on purpose: a false miss lowers the number, a false hit would hide a gap. A lead
    missing either field is not judged at all, so it cannot count as a miss.
    """
    by_name = [
        (key, lead.company, lead.title)
        for key, lead in enumerate(leads)
        if lead.company and lead.title
    ]
    held = await discovery_q.held_jobs(conn, by_name)
    judged = {key for key, _, _ in by_name}
    return [
        lead.model_copy(
            update={"held": key in held if key in judged else None, "held_job_id": held.get(key)}
        )
        for key, lead in enumerate(leads)
    ]


async def reader_searches(conn) -> list[Search]:
    """Every active reader's searches: their cached expanded queries, else the deterministic ones.

    Never an LLM call: an expansion is paid for by the match, and this only reuses it.
    """
    planned: dict[Search, None] = {}
    for user in await users_q.list_users(conn):
        row = await users_q.get_profile(conn, user.id) if user.is_active else None
        if row is None:
            continue
        profile = profile_from_row(row)
        cached = await match_q.get_query_expansion(
            conn, user.id, profile.version, versions.QUERY_EXPANSION_VERSION
        )
        queries = cached.queries if cached else expand.deterministic_queries(profile)
        for search in searches_for(profile, queries):
            planned.setdefault(search, None)
    return list(planned)


async def run(settings: Settings | None = None, *, force: bool = False) -> Harvest | None:
    """One pass, when a day has gone by since the last one. None when not due or switched off."""
    settings = settings or get_settings()
    if settings.linkedin_max_requests <= 0:
        return None
    now = datetime.now(UTC)
    async with connect() as conn:
        last = await ingest_q.last_sweep_started(conn, SOURCE)
        if not force and last is not None and now - last < EVERY:
            return None
        searches = await reader_searches(conn)
        known = await discovery_q.payload_values(conn, origin=SOURCE, key="linkedin_id")
        sweep_id, _ = await ingest_q.start_sweep(conn, SOURCE)

    found = Harvest()
    if searches:
        async with PoliteClient() as client:
            found = await harvest(
                client.paced(DELAY_SECONDS, attempts=1),
                searches,
                max_requests=settings.linkedin_max_requests,
                known=known,
            )
    async with connect() as conn:
        found.leads = await judge(conn, found.leads)
    written = await record_leads(found.leads)

    async with connect() as conn:
        await ingest_q.finish_sweep(
            conn,
            sweep_id,
            ok=not (found.blocked or found.error),
            complete=False,
            partitions_total=found.searches_planned,
            partitions_done=found.searches_done,
            partitions_overflowed=0,
            documents_seen=found.cards,
            jobs_upserted=0,
            jobs_closed=0,
            error=found.blocked or found.error,
            blocked=found.blocked is not None,
        )
    log.info(
        "linkedin: %d request(s), %d card(s), %d new lead(s), %d already held",
        found.requests, found.cards, written, sum(bool(lead.held) for lead in found.leads),
    )
    return found
