"""Jobicy remote-jobs API — network only, no parsing.

robots.txt (jobicy.com, checked 2026-09-09): `Content-Signal: ai-train=yes, search=yes,
ai-input=yes`. Its body asks that apply links go to the original posting; the digest links `url`.

One capped page, no cursor -- which is why it closes nothing even on a backfill.
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

# The documented maximum for one request. No cursor, so this is the whole sweep.
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
            next_cursor=lambda payload: None,
            identify=lambda row: row.get("id"),
            published_at=lambda row: iso_datetime(row.get("pubDate")),
            backfill=backfill,
            delta_window=self.delta_window,
            expected_for=lambda payload: (payload or {}).get("jobCount"),
            # One capped page is not the whole live set. Without this a backfill would treat the
            # newest 50 postings as the corpus and retire every other Jobicy posting we hold.
            complete_on_backfill=False,
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Jobicy listings already carry the description; fetch_detail must not be called."
        )
