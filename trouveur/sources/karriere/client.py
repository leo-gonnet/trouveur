"""karriere.at, Austria's largest job board — network only, no field interpretation.

robots.txt (www.karriere.at, checked 2026-10-26): `Disallow:` is empty for every agent but two
SEO crawlers. Probed the same day:

- `/jobs?page=N` answers JSON only when asked with both `Accept: application/json` and
  `X-Requested-With: XMLHttpRequest`. Without them it is the same page as ~200 KB of HTML.
- 15 rows a page, newest first, and the walk ends at page 500: page 550 is HTTP 410. That is
  7,500 rows where the job sitemap lists ~13,000 live postings, so not even a backfill sees the
  whole board, and nothing is ever closed on one.
- `loadMoreJobsButton.next` is the next page number, and `false` on the last.
- A row's date is prose ("Heute veröffentlicht", "vor 3 Tagen veröffentlicht"). The real date,
  the full description and a structured address are in the posting page's JSON-LD, which the
  detail phase reads. A posting that is gone answers HTTP 404.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from selectolax.parser import HTMLParser

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.errors import FetchError, SourceBlocked
from trouveur.sources.feed import require_list, sweep_feed
from trouveur.sources.http import PoliteClient

SOURCE = "karriere"

_JOBS_URL = "https://www.karriere.at/jobs"
_POSTING_URL = "https://www.karriere.at/jobs/{id}"
_JSON_HEADERS = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

# The rows dated up to "vor 2 Tagen", about 70 pages a night, so one missed night is still
# covered; seven days would be the whole reachable feed every night. The prose is whole days and
# "vor 2 Tagen" lands exactly on a two-day edge, which cut the walk at yesterday's rows when probed
# live -- hence a window of three.
_DELTA_WINDOW = timedelta(days=3)

_DAYS_AGO = re.compile(r"vor (\d+) (tag|woche|monat)", re.IGNORECASE)
_UNIT_DAYS = {"tag": 1, "woche": 7, "monat": 30}


class KarriereSource:
    name = SOURCE
    requires_detail = True
    tenant_scoped = False

    def __init__(self, delta_window: timedelta | None = None) -> None:
        self.delta_window = delta_window or _DELTA_WINDOW

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        now = datetime.now(UTC)

        async def fetch(cursor: object | None) -> Any:
            return await client.get_json(
                _JOBS_URL, params={"page": str(cursor or 1)}, headers=_JSON_HEADERS
            )

        return await sweep_feed(
            sink,
            source=SOURCE,
            fetch=fetch,
            extract=_rows,
            next_cursor=_next_page,
            identify=lambda row: row.get("id"),
            published_at=lambda row: published_at(row.get("date"), now),
            backfill=backfill,
            delta_window=self.delta_window,
            complete_on_backfill=False,
        )

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> RawDocument | None:
        response = await client.get(_POSTING_URL.format(id=external_id))
        if response.status_code in {404, 410}:
            return None
        if response.status_code != 200:
            raise FetchError(
                f"karriere.at posting {external_id} returned HTTP {response.status_code}."
            )
        posting = job_posting(response.text)
        if posting is None:
            raise FetchError(
                f"karriere.at posting {external_id} has no JSON-LD JobPosting; the page changed."
            )
        return RawDocument(
            source=SOURCE, external_id=external_id, kind=DocumentKind.DETAIL, payload=posting
        )


def _rows(payload: Any) -> list[dict]:
    """The rows of one page. A body without the list is a block, not an empty page: the last
    page still carries it."""
    listing = ((payload or {}).get("data") or {}).get("jobsSearchList") if isinstance(
        payload, dict
    ) else None
    items = (listing or {}).get("activeItems")
    if not isinstance(items, dict):
        raise SourceBlocked("karriere.at returned a body without its job list.")
    return [
        item["jobsItem"]
        for item in require_list(items, "items", source=SOURCE)
        if isinstance(item.get("jobsItem"), dict)
    ]


def _next_page(payload: Any) -> object | None:
    listing = ((payload or {}).get("data") or {}).get("jobsSearchList") or {}
    following = (listing.get("loadMoreJobsButton") or {}).get("next")
    return following if isinstance(following, int) and not isinstance(following, bool) else None


def published_at(text: Any, now: datetime) -> datetime | None:
    """The listing's prose date, to the day. Only the sweep's window reads it."""
    if not isinstance(text, str):
        return None
    lowered = text.lower()
    if lowered.startswith("heute"):
        return now
    if lowered.startswith("gestern"):
        return now - timedelta(days=1)
    match = _DAYS_AGO.search(lowered)
    if match is None:
        return None
    return now - timedelta(days=int(match.group(1)) * _UNIT_DAYS[match.group(2)])


def job_posting(html: str) -> dict | None:
    """The page's JSON-LD JobPosting, the one structured record a posting page carries."""
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        for candidate in data if isinstance(data, list) else [data]:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    return None
