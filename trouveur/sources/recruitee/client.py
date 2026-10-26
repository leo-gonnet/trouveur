"""Recruitee careers-site API — network only, no parsing.

Probed 2026-10-26, no key: `https://<board>.recruitee.com/api/offers/` is a company's complete
live board in one response, description included, so there is no detail phase. An unknown board
is HTTP 404 with a JSON body. Every board is a subdomain of one provider, which `throttle_key`
already treats as one politeness budget.
"""

from __future__ import annotations

from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.board import sweep_boards
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

SOURCE = "recruitee"

_BOARD_URL = "https://{scope}.recruitee.com/api/offers/"


class RecruiteeSource:
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
            extract=_offers,
            identify=lambda row: row.get("id"),
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Recruitee offers already carry the description; fetch_detail must not be called."
        )


def _offers(payload: Any) -> list[dict]:
    """A board's offers. A body without the key is no answer about the board, and read as an
    empty one it would close every posting the board holds."""
    rows = payload.get("offers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise FetchError("Recruitee returned a body without an 'offers' list.")
    return [row for row in rows if isinstance(row, dict)]
