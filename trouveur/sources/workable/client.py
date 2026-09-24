"""Workable's public job board — network only, no parsing.

robots.txt (jobs.workable.com, checked 2026-09-09): `Content-Signal: search=yes, ai-input=yes,
ai-train=no`, disallowing `/search`, `/search*?*`, `/profile*`. The `/api/` path used here is not
disallowed. We index and retrieve; we do not train.

The page size is fixed at 20 and unknown parameters are silently ignored -- see `fetch`.
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
    tenant_scoped = False

    def __init__(self, delta_window: timedelta | None = None) -> None:
        self.delta_window = delta_window or timedelta(days=7)

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        async def fetch(cursor: object | None) -> Any:
            # Never send a page-size parameter: an unsupported `limit` returns an empty body
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
