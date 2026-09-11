"""Breezy HR public board — network only, no parsing.

robots.txt (breezy.hr, checked 2026-09-09): `Allow: /` with `Disallow: /api/`, `/app/`, `/m/`,
`/tags/`. This adapter uses `/json`, which is not among the disallowed paths. Boards live on
per-tenant subdomains, so the budget is owed to breezy.hr as a whole -- see http.throttle_key.

Shape verified live on 2026-09-09:

  - one request returns the tenant's COMPLETE live board, so the response is itself the seen-set;
  - the body is a BARE TOP-LEVEL ARRAY, as with Lever;
  - an unknown board returns HTTP 404 with an HTML page, not JSON, so the status must be checked
    before the body is decoded.
"""

from __future__ import annotations

from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.board import sweep_boards
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

SOURCE = "breezy"

_BOARD_URL = "https://{scope}.breezy.hr/json"


class BreezySource:
    name = SOURCE
    requires_detail = False
    tenant_scoped = True

    def __init__(self, boards: list[str] | None = None) -> None:
        self.boards = boards or []

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        # A board dump is always the complete live set, so a backfill and a daily run are the same
        # request. The flag is accepted for protocol conformance and deliberately unused.
        return await sweep_boards(
            client,
            sink,
            source=SOURCE,
            scopes=self.boards,
            url_for=lambda scope: _BOARD_URL.format(scope=scope),
            extract=_extract,
            identify=lambda row: row.get("id"),
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Breezy listings already carry the description; fetch_detail must not be called."
        )


def _extract(payload: Any) -> list[dict]:
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]
