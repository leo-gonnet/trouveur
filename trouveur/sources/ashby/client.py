"""Ashby job board API — network only, no parsing.

robots.txt (api.ashbyhq.com, checked 2026-09-09): the host serves none, so no restriction is
expressed. `jobs.ashbyhq.com` DOES disallow `/api/`, but that is the hosted HTML board on a
different host; do not merge the two.
"""

from __future__ import annotations

from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.board import sweep_boards
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

SOURCE = "ashby"

_BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{scope}"

# Without this, salary arrives only as a rendered string that cannot be parsed back to numbers.
_PARAMS = {"includeCompensation": "true"}


class AshbySource:
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

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> None:
        raise FetchError(
            "Ashby listings already carry the description; fetch_detail must not be called."
        )


def _extract(payload: Any) -> list[dict]:
    rows = (payload or {}).get("jobs") or []
    # `isListed` false means the posting exists but is not published on the board.
    return [row for row in rows if isinstance(row, dict) and row.get("isListed") is not False]
