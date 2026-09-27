"""Workable's public job board — network only, no parsing.

robots.txt (jobs.workable.com, checked 2026-09-09): `Content-Signal: search=yes, ai-input=yes,
ai-train=no`, disallowing `/search`, `/search*?*`, `/profile*`. The `/api/` path used here is not
disallowed. We index and retrieve; we do not train.

The page size is fixed at 20 and unknown parameters are silently ignored -- see `fetch`.

Re-probed 2026-09-27: the endpoint, its shape and its limits are unchanged; `day_range` and
`pageToken` were verified against it directly.
"""

from __future__ import annotations

import math
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
            params: dict[str, Any] = {"query": ""}
            if not backfill:
                # The window is applied HERE, in the request, and not by the caller. The feed is
                # not newest-first: it leads with boosted postings ordered by when their boost
                # expires, so a posting from four weeks ago sits in front of everything published
                # today and a client-side break on the first out-of-window row stopped the sweep
                # after ~30 postings. `day_range` is validated server-side (a non-number is a 400,
                # a negative one is a 400) and 0 means no filter at all, so the value is never
                # defaulted silently.
                params["day_range"] = str(_day_range(self.delta_window))
            if cursor:
                # The REQUEST parameter is `pageToken`; the response names the same value
                # `nextPageToken`. Sending the response's name back is an unknown parameter,
                # which this API ignores in silence -- so every request returned page one, the
                # walk never advanced and never exhausted its window, and it ran to
                # `feed.MAX_PAGES`. That is what earned the HTTP 429 the sweep reported.
                params["pageToken"] = str(cursor)
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
            server_side_window=True,
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Workable listings already carry the description; fetch_detail must not be called."
        )


def _day_range(window: timedelta) -> int:
    """`day_range` in whole days, rounded up, and never 0 -- 0 is the whole 170k-posting corpus."""
    return max(1, math.ceil(window.total_seconds() / 86_400))
