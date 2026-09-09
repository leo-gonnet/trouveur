"""Jobicy remote-jobs API — network only, no parsing.

robots.txt (jobicy.com, checked 2026-09-09): `Content-Signal: ai-train=yes, search=yes,
ai-input=yes` -- an explicit grant for indexing and for use as model input. The response body also
asks that Jobicy be credited with a link and that apply buttons go to the original posting URL;
the digest links to `url`, which is that posting.

Shape verified live on 2026-09-09:

  - a small feed (thousands, not hundreds of thousands) of remote postings, newest first;
  - `count` caps the page; there is no cursor, so the feed is read in one pass rather than paged;
  - salary arrives as numbers with a currency and period, and the description is inline HTML.

Because there is no cursor, the delta window cannot shorten the request -- but the whole feed is
one page, so the ordinary run costs a single call either way.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.errors import FetchError
from trouveur.sources.feed import require_list, sweep_feed
from trouveur.sources.http import PoliteClient
from trouveur.sources.parse import iso_datetime

SOURCE = "jobicy"

_JOBS_URL = "https://jobicy.com/api/v2/remote-jobs"

# The documented maximum for one request. The feed has no cursor, so this is the whole sweep.
_COUNT = 50


class JobicySource:
    name = SOURCE
    requires_detail = False
    tenant_scoped = False

    def __init__(self, delta_window: timedelta | None = None) -> None:
        self.delta_window = delta_window or timedelta(days=7)

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        async def fetch(cursor: object | None) -> Any:
            return await client.get_json(_JOBS_URL, params={"count": str(_COUNT)})

        return await sweep_feed(
            sink,
            source=SOURCE,
            fetch=fetch,
            extract=lambda payload: require_list(payload, "jobs", source=SOURCE),
            # One page only: returning no cursor is what stops the sweep after the first call.
            next_cursor=lambda payload: None,
            identify=lambda row: row.get("id"),
            published_at=lambda row: iso_datetime(row.get("pubDate")),
            backfill=backfill,
            delta_window=self.delta_window,
            expected_for=lambda payload: (payload or {}).get("jobCount"),
            # One capped page is not the whole live set, so this sweep may never close anything.
            # Without this a backfill would treat the newest 50 postings as the entire corpus and
            # retire every other Jobicy posting we hold.
            complete_on_backfill=False,
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Jobicy listings already carry the description; fetch_detail must not be called."
        )
