"""Lever postings API — network only, no parsing.

robots.txt (api.lever.co, checked 2026-09-09): `User-agent: * / Allow: / / Crawl-delay: 1`. The
host explicitly permits crawling and asks for one second between requests, which is exactly the
project's default politeness budget.

Shape verified live on 2026-09-09:

  - one request returns the tenant's COMPLETE live board, so the response is itself the seen-set;
  - the body is a BARE TOP-LEVEL ARRAY, not an object with a `jobs` key. Reaching for `["jobs"]`
    raises TypeError on a list, which at least fails loudly -- but a `.get("jobs")` would return
    None and report an empty board;
  - `descriptionPlain` carries the full text, so there is no detail phase.
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
            params=_PARAMS,
        )

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Lever listings already carry the description; fetch_detail must not be called."
        )


def _extract(payload: Any) -> list[dict]:
    # The response is the list itself. Guarded rather than assumed, so a future envelope shows up
    # as an empty sweep to investigate instead of a TypeError mid-batch.
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]
