"""Personio XML job feed — network only, no field interpretation.

robots.txt (jobs.personio.de, checked 2026-09-09): empty, so no restriction is expressed.

An unknown tenant answers HTTP 429 with an HTML challenge page, not 404 -- see `_sweep_board`.
The feed states no posting URL, so the tenant must survive in the external id.

It keeps its own board loop rather than `sweep_boards` because that content-type check has to run
before the status gate the shared sweep applies first.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, ScopeResult, SweepOutcome
from trouveur.sources.board import BATCH, scoped_id
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

SOURCE = "personio"

_BOARD_URL = "https://{scope}.jobs.personio.de/xml"

class PersonioSource:
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
                seen = await self._sweep_board(client, sink, scope)
            except FetchError as exc:
                outcome.errors.append(f"scope={scope}: {exc}")
                outcome.scope_results.append(ScopeResult(scope=scope, ok=False, error=str(exc)))
                continue
            outcome.documents += seen
            outcome.partitions_done += 1
            outcome.expected = (outcome.expected or 0) + seen
            outcome.scope_results.append(ScopeResult(scope=scope, ok=True, documents=seen))
            outcome.closable_scopes.append(scope)
        return outcome

    async def _sweep_board(
        self, client: PoliteClient, sink: DocumentSink, scope: str
    ) -> int:
        response = await client.get(_BOARD_URL.format(scope=scope))
        content_type = response.headers.get("content-type", "")
        # An unknown tenant answers 429 with an HTML challenge page: the body, not the status,
        # is what tells a bad slug apart from throttling PoliteClient would retry.
        if "xml" not in content_type.lower():
            raise FetchError(
                f"Personio board {scope!r} returned HTTP {response.status_code} with "
                f"content-type {content_type!r} instead of XML; the tenant most likely does not "
                "exist. A 429 here is a bad slug, not throttling."
            )
        if response.status_code != 200:
            raise FetchError(
                f"Personio board {scope!r} returned HTTP {response.status_code}."
            )

        positions = _positions(response.text, scope)
        seen = 0
        for start in range(0, len(positions), BATCH):
            documents = [
                RawDocument(
                    source=SOURCE,
                    external_id=scoped_id(scope, position["id"]),
                    kind=DocumentKind.LISTING,
                    scope=scope,
                    payload=position,
                )
                for position in positions[start : start + BATCH]
            ]
            if documents:
                await sink(documents)
                seen += len(documents)
        return seen


def _positions(text: str, scope: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise FetchError(
            f"Personio board {scope!r} returned XML that does not parse: {exc}."
        ) from exc
    records = []
    for node in root.iter("position"):
        record = element_to_dict(node)
        if isinstance(record, dict) and str(record.get("id") or "").strip():
            records.append(record)
    return records


def element_to_dict(node: ElementTree.Element) -> Any:
    """One XML element as JSON-compatible data. Repeated sibling tags become a list, so a board
    with one section and a board with several decode to the same shape."""
    children = list(node)
    if not children:
        return (node.text or "").strip()

    record: dict[str, Any] = {}
    for child in children:
        value = element_to_dict(child)
        if child.tag in record:
            existing = record[child.tag]
            record[child.tag] = existing if isinstance(existing, list) else [existing]
            record[child.tag].append(value)
        else:
            record[child.tag] = value
    return record
