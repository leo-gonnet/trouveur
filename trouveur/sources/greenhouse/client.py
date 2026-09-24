"""Greenhouse job boards API — network only, no parsing.

robots.txt (boards-api.greenhouse.io, checked 2026-09-08): the only rule is `Disallow: /embed/`,
so `/v1/boards/` is explicitly permitted. No authentication.

One request returns a tenant's COMPLETE live board, and `?content=true` includes the description,
so there is no detail phase. There is no index of tenants anywhere: `source_tenant` is the only
list of boards that exists, and an unknown slug 404s.
"""

from __future__ import annotations

from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.board import scoped_id as external_id
from trouveur.sources.board import sweep_boards
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

SOURCE = "greenhouse"

_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{scope}/jobs"
_PARAMS = {"content": "true"}


class GreenhouseSource:
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
            expected_for=_expected,
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Greenhouse listings already carry the description; fetch_detail must not be called."
        )


def _extract(payload: Any) -> list[dict]:
    rows = (payload or {}).get("jobs") or []
    return [row for row in rows if isinstance(row, dict)]


def _expected(payload: Any) -> int | None:
    total = ((payload or {}).get("meta") or {}).get("total")
    return int(total) if isinstance(total, int) else None


__all__ = ["SOURCE", "GreenhouseSource", "external_id"]
