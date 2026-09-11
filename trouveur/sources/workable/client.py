"""Workable's public job board — network only, no parsing.

robots.txt (jobs.workable.com, checked 2026-09-09): `Content-Signal: search=yes, ai-input=yes,
ai-train=no`, with `Disallow: /search`, `/search*?*`, `/profile*`. The `/api/` path this adapter
uses is not disallowed. We index and retrieve; we do not train.

This is the one global source with no tenant registry at all, which is why it is worth its own
adapter even though Workable also has a per-tenant board API.

Shape verified live on 2026-09-09:

  - ~170 000 postings in one corpus, ordered NEWEST FIRST, paged by opaque `nextPageToken`;
  - **the page size is fixed at 20 and cannot be raised.** Sending `limit=100` returns HTTP 200
    with no `jobs` key and no token at all -- an empty result that reads as "the corpus ended"
    rather than as a rejected parameter. So no page-size parameter is ever sent;
  - unknown query parameters are silently ignored rather than rejected, so a filter that looks
    like it applied may not have. Nothing here relies on one;
  - `location` is already structured as city/subregion/countryName, and the description is inline,
    so there is no detail phase.

Sweeping the whole corpus is 8 500 requests, so the daily run is a delta over the recency
ordering; see feed.sweep_feed for why that means it may close nothing.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.errors import FetchError
from trouveur.sources.feed import require_list, sweep_feed
from trouveur.sources.http import PoliteClient
from trouveur.sources.parse import iso_datetime

SOURCE = "workable"

_JOBS_URL = "https://jobs.workable.com/api/v1/jobs"


class WorkableSource:
    name = SOURCE
    requires_detail = False
    # No tenant list: this endpoint is the whole corpus.
    tenant_scoped = False

    def __init__(self, delta_window: timedelta | None = None) -> None:
        self.delta_window = delta_window or timedelta(days=7)

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        async def fetch(cursor: object | None) -> Any:
            # Deliberately no page-size parameter: an unsupported `limit` returns an empty body
            # that is indistinguishable from the end of the corpus.
            params = {"query": ""}
            if cursor:
                params["nextPageToken"] = str(cursor)
            return await client.get_json(_JOBS_URL, params=params)

        return await sweep_feed(
            sink,
            source=SOURCE,
            fetch=fetch,
            extract=lambda payload: require_list(payload, "jobs", source=SOURCE),
            next_cursor=lambda payload: (payload or {}).get("nextPageToken") or None,
            identify=lambda row: row.get("id"),
            published_at=lambda row: iso_datetime(row.get("created")),
            backfill=backfill,
            delta_window=self.delta_window,
            expected_for=lambda payload: (payload or {}).get("totalSize"),
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Workable listings already carry the description; fetch_detail must not be called."
        )
