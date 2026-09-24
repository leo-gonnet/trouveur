"""Breezy HR public board — network only, no parsing.

robots.txt (breezy.hr, checked 2026-09-09): `Allow: /` with `Disallow: /api/`, `/app/`, `/m/`,
`/tags/`. This adapter uses `/json`, which is not disallowed.

The body is a BARE TOP-LEVEL ARRAY.
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
