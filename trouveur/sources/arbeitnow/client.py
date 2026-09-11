"""Arbeitnow job board API — network only, no parsing.

robots.txt (www.arbeitnow.com, checked 2026-09-09): `User-agent: * / Disallow:` -- an empty rule,
which permits everything. The API's own `meta.terms` asks that it not be abused and that callers
link back to the site; the digest links to the posting, and the shared politeness budget applies.

Shape verified live on 2026-09-09:

  - a free, unauthenticated feed of the whole corpus, ordered NEWEST FIRST by `created_at`;
  - 250 postings a page, paged by a full URL in `links.next` rather than a cursor parameter;
  - the description is inline HTML, so there is no detail phase;
  - the feed is DACH-weighted, which is where it earns its place among the global sources.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.errors import FetchError
from trouveur.sources.feed import require_list, sweep_feed
from trouveur.sources.http import PoliteClient
from trouveur.sources.parse import epoch_datetime

SOURCE = "arbeitnow"

_JOBS_URL = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowSource:
    name = SOURCE
    requires_detail = False
    tenant_scoped = False

    def __init__(self, delta_window: timedelta | None = None) -> None:
        self.delta_window = delta_window or timedelta(days=7)

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        async def fetch(cursor: object | None) -> Any:
            # Pagination hands back a complete URL, not a page number.
            return await client.get_json(str(cursor) if cursor else _JOBS_URL)

        return await sweep_feed(
            sink,
            source=SOURCE,
            fetch=fetch,
            extract=lambda payload: require_list(payload, "data", source=SOURCE),
            next_cursor=_next_url,
            identify=lambda row: row.get("slug"),
            published_at=lambda row: epoch_datetime(row.get("created_at")),
            backfill=backfill,
            delta_window=self.delta_window,
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Arbeitnow listings already carry the description; fetch_detail must not be called."
        )


def _next_url(payload: Any) -> str | None:
    links = (payload or {}).get("links")
    if not isinstance(links, dict):
        return None
    return links.get("next") or None
