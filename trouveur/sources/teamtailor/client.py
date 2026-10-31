"""Teamtailor career-site RSS — network only, no field interpretation.

Probed 2026-10-26, no key:

- `https://<board>.teamtailor.com/jobs.rss` stops at 100 items, whatever the board holds: a
  321-job board answered exactly 100, and read as one response it looks complete.
- `per_page` and `offset` page it. `page` is ignored and answers the first 100 again.
- An unknown board is HTTP 404 with an HTML page.
- An item carries the description; its places sit in the `https://teamtailor.com/locations`
  namespace, which the decoder drops from the tag names.

A board on a custom domain is not addressed: it would be paced as its own provider, though
Teamtailor serves it.
"""

from __future__ import annotations

from xml.etree import ElementTree

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, ScopeResult, SweepOutcome
from trouveur.sources.board import BATCH, scoped_id
from trouveur.sources.errors import FetchError, SourceBlocked
from trouveur.sources.http import PoliteClient
from trouveur.sources.parse import element_to_dict

SOURCE = "teamtailor"

_FEED_URL = "https://{scope}.teamtailor.com/jobs.rss"

PAGE_SIZE = 200
# Backstop for a feed that never hands out a short page.
MAX_OFFSET = 20_000


class TeamtailorSource:
    name = SOURCE
    requires_detail = False
    tenant_scoped = True

    def __init__(self, boards: list[str] | None = None) -> None:
        self.boards = boards or []

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        outcome = SweepOutcome(partitions_total=len(self.boards), expected=0)
        for scope in self.boards:
            try:
                seen, complete = await self._sweep_board(client, sink, scope)
            # A FetchError too: caught below, a block would never stop the source.
            except SourceBlocked:
                raise
            except FetchError as exc:
                outcome.errors.append(f"scope={scope}: {exc}")
                outcome.scope_results.append(ScopeResult(scope=scope, ok=False, error=str(exc)))
                continue
            outcome.documents += seen
            outcome.partitions_done += 1
            outcome.expected = (outcome.expected or 0) + seen
            outcome.scope_results.append(ScopeResult(scope=scope, ok=True, documents=seen))
            if not complete:
                outcome.partitions_overflowed += 1
                continue
            outcome.closable_scopes.append(scope)
        return outcome

    async def _sweep_board(
        self, client: PoliteClient, sink: DocumentSink, scope: str
    ) -> tuple[int, bool]:
        url = _FEED_URL.format(scope=scope)
        seen_ids: set[str] = set()
        for offset in range(0, MAX_OFFSET, PAGE_SIZE):
            response = await client.get(url, params={"per_page": PAGE_SIZE, "offset": offset})
            if response.status_code == 404:
                raise FetchError(f"Teamtailor board {scope!r} does not exist (HTTP 404).")
            if response.status_code != 200:
                raise FetchError(
                    f"Teamtailor board {scope!r} returned HTTP {response.status_code} at offset "
                    f"{offset}."
                )
            items = _items(response.text, scope)
            fresh = [item for item in items if item["guid"] not in seen_ids]
            # A page of items already seen is the offset being ignored, not the end.
            if items and not fresh:
                return len(seen_ids), False
            seen_ids.update(item["guid"] for item in fresh)
            for start in range(0, len(fresh), BATCH):
                await sink(
                    [
                        RawDocument(
                            source=SOURCE,
                            external_id=scoped_id(scope, item["guid"]),
                            kind=DocumentKind.LISTING,
                            scope=scope,
                            payload=item,
                        )
                        for item in fresh[start : start + BATCH]
                    ]
                )
            if len(items) < PAGE_SIZE:
                return len(seen_ids), True
        return len(seen_ids), False

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "Teamtailor items already carry the description; fetch_detail must not be called."
        )


def _items(text: str, scope: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise FetchError(
            f"Teamtailor board {scope!r} returned a feed that does not parse: {exc}."
        ) from exc
    records = []
    for node in root.iter("item"):
        record = element_to_dict(node)
        if isinstance(record, dict) and str(record.get("guid") or "").strip():
            record["guid"] = str(record["guid"]).strip()
            records.append(record)
    return records
