"""Rippling ATS public board — network only, no parsing.

robots.txt (api.rippling.com, checked 2026-09-09): the host serves no robots.txt -- the request
returns a plain "Not Found!" body -- so no restriction is expressed for it.

Shape verified live on 2026-09-09:

  - one request returns the tenant's COMPLETE live board, so the response is itself the seen-set;
  - the body is a BARE TOP-LEVEL ARRAY, as with Lever and Breezy;
  - **the listing carries only uuid, name, department, url and workLocation.** There is no
    description, no company and no date on it, so unlike every other board source here this one
    genuinely needs a detail fetch per posting -- the listing alone would produce a row the
    reranker cannot read and the recency filters cannot place.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.board import sweep_boards
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

SOURCE = "rippling"

_BOARD_URL = "https://api.rippling.com/platform/api/ats/v1/board/{scope}/jobs"


class RipplingSource:
    name = SOURCE
    # The listing has no description, so every posting costs one extra request. The pipeline
    # queues and drains those at a polite rate rather than making the sweep pay for them inline.
    requires_detail = True
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
            identify=lambda row: row.get("uuid"),
        )

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> RawDocument | None:
        # The board slug is part of the detail URL, and the external id is the only place the
        # sweep recorded it -- see board.scoped_id.
        scope, _, job_id = external_id.partition(":")
        if not scope or not job_id:
            raise FetchError(
                f"Rippling external id {external_id!r} is not 'board:uuid'; it cannot address a "
                "detail request."
            )
        response = await client.get(f"{_BOARD_URL.format(scope=scope)}/{job_id}")
        # A retired posting 404s. That is the normal end of a listing's life, not a failure.
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise FetchError(
                f"Rippling detail for {external_id} returned HTTP {response.status_code}."
            )
        payload: Any = response.json()
        if not isinstance(payload, dict):
            raise FetchError(f"Rippling detail for {external_id} was not a JSON object.")
        return RawDocument(
            source=SOURCE,
            external_id=external_id,
            kind=DocumentKind.DETAIL,
            scope=scope,
            payload=payload,
        )


def _extract(payload: Any) -> list[dict]:
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]
