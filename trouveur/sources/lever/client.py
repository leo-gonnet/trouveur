"""Lever postings API — network only, no parsing.

robots.txt (api.lever.co, checked 2026-09-09): `Allow: /` with `Crawl-delay: 1`.

The body is a BARE TOP-LEVEL ARRAY, not an object with a `jobs` key.
"""

from __future__ import annotations

from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.board import sweep_boards
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

SOURCE = "lever"

_BOARD_URL = "https://api.lever.co/v0/postings/{scope}"

# Without mode=json the endpoint renders an HTML board instead of returning data.
_PARAMS = {"mode": "json"}


class LeverSource:
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
            params=_PARAMS,
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Lever listings already carry the description; fetch_detail must not be called."
        )


def _extract(payload: Any) -> list[dict]:
    # Guarded, so an envelope appearing later is an empty sweep rather than a TypeError.
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]
