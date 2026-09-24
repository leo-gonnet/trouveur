"""Himalayas remote-jobs API — network only, no parsing.

robots.txt (himalayas.app, checked 2026-09-09): `Allow: /`, disallowing only paginated HTML views
and `/apply`. The JSON API path is not among them.

There is no `id` field; `guid` is the identity.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.errors import FetchError
from trouveur.sources.feed import require_list, sweep_feed
from trouveur.sources.http import PoliteClient
from trouveur.sources.parse import epoch_datetime

SOURCE = "himalayas"

_JOBS_URL = "https://himalayas.app/jobs/api"


class HimalayasSource:
    name = SOURCE
    requires_detail = False
    tenant_scoped = False

    def __init__(self, delta_window: timedelta | None = None) -> None:
        self.delta_window = delta_window or timedelta(days=7)

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        async def fetch(cursor: object | None) -> Any:
            params = {"limit": "50"}
            if cursor:
                params["cursor"] = str(cursor)
            return await client.get_json(_JOBS_URL, params=params)

        return await sweep_feed(
            sink,
            source=SOURCE,
            fetch=fetch,
            extract=lambda payload: require_list(payload, "jobs", source=SOURCE),
            next_cursor=lambda payload: (payload or {}).get("nextCursor") or None,
            identify=lambda row: row.get("guid"),
            published_at=lambda row: epoch_datetime(row.get("pubDate")),
            backfill=backfill,
            delta_window=self.delta_window,
            expected_for=lambda payload: (payload or {}).get("totalCount"),
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Himalayas listings already carry the description; fetch_detail must not be called."
        )
