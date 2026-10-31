"""EURES, the EU job mobility portal — network only, no parsing.

Every national employment service feeds it, so one public JSON search reaches most of their
postings. Probed 2026-10-26: no key, no cookie, and a body holding only the fields below.

- `resultsPerPage` is at most 50 and `page` at most 200: 51 a page, or page 201, is HTTP 400.
  One query therefore reaches 10,000 rows, and a country is split by region to stay under it.
- `locationCodes` filters by a country ("de") or a NUTS 2 region ("de30") ONLY. A NUTS 1 or
  NUTS 3 code is accepted and answers zero rows, never an error, so the split codes are taken
  from the response's own facet rather than built.
- A country's facet lists its NUTS 2 regions AND "<country>-NS", the postings that name no
  region. The latter filters like a region; leave it out and those postings are never seen.
- `publicationPeriod` counts back from the moment of the request over the last modification,
  not the creation: national services re-send their batches, so a posting created months ago
  arrives again inside the window. An unknown period is HTTP 500.
- The search rows carry the full description. The detail endpoint adds only the apply
  instructions, so there is no detail phase.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import GLOBAL_SCOPE, DocumentSink, SweepOutcome
from trouveur.sources.board import BATCH
from trouveur.sources.errors import FetchError, SourceBlocked
from trouveur.sources.eures import nuts
from trouveur.sources.feed import require_list
from trouveur.sources.http import PoliteClient

log = logging.getLogger(__name__)

SOURCE = "eures"

_SEARCH_URL = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"

PAGE_SIZE = 50
RESULT_WINDOW = 10_000

# Three days, not one: a service's batch lands at its own hour, and a nightly run that starts a
# little later than the night before would miss a "last day" batch for good.
_DAILY_PERIOD = "LAST_THREE_DAYS"
_BACKFILL_PERIOD = "LAST_WEEK"


class EuresSource:
    name = SOURCE
    requires_detail = False
    tenant_scoped = False

    def __init__(self, countries: list[str] | None = None) -> None:
        self.countries = countries or []

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        period = _BACKFILL_PERIOD if backfill else _DAILY_PERIOD
        # A delta never observes a whole country, so nothing may be closed on it.
        outcome = SweepOutcome(closable_scopes=[], expected=0)
        seen: set[str] = set()
        for iso in self.countries:
            code = nuts.country_code(iso)
            if code is None:
                continue
            try:
                await self._sweep_country(client, sink, code.lower(), period, outcome, seen)
            # A FetchError too: caught below, a block would never stop the source.
            except SourceBlocked:
                raise
            except FetchError as exc:
                outcome.errors.append(f"country={code}: {exc}")
                log.warning("eures: country %s failed: %s", code, exc)
        return outcome

    async def _sweep_country(
        self,
        client: PoliteClient,
        sink: DocumentSink,
        code: str,
        period: str,
        outcome: SweepOutcome,
        seen: set[str],
    ) -> None:
        first = await _search(client, [code], period, 1)
        total = _total(first)
        outcome.expected = (outcome.expected or 0) + total
        if total <= RESULT_WINDOW:
            partitions = [(code, total, first)]
        else:
            partitions = [(region, count, None) for region, count in _regions(first, code)]

        for region, count, page_one in partitions:
            outcome.partitions_total += 1
            try:
                await self._walk(client, sink, region, count, period, page_one, outcome, seen)
            except SourceBlocked:
                raise
            except FetchError as exc:
                outcome.errors.append(f"region={region}: {exc}")
                log.warning("eures: region %s failed: %s", region, exc)
                continue
            outcome.partitions_done += 1

    async def _walk(
        self,
        client: PoliteClient,
        sink: DocumentSink,
        code: str,
        count: int,
        period: str,
        page_one: Any,
        outcome: SweepOutcome,
        seen: set[str],
    ) -> None:
        if count > RESULT_WINDOW:
            outcome.partitions_overflowed += 1
            log.warning(
                "eures: %s holds %d postings but only %d are reachable; the rest are not being "
                "collected",
                code, count, RESULT_WINDOW,
            )
        pages = min(math.ceil(count / PAGE_SIZE), RESULT_WINDOW // PAGE_SIZE)
        for page in range(1, pages + 1):
            payload = page_one if page == 1 and page_one is not None else (
                await _search(client, [code], period, page)
            )
            rows = require_list(payload, "jvs", source=SOURCE)
            documents = []
            for row in rows:
                job_id = str(row.get("id") or "").strip()
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                documents.append(
                    RawDocument(
                        source=SOURCE,
                        external_id=job_id,
                        kind=DocumentKind.LISTING,
                        scope=GLOBAL_SCOPE,
                        payload=row,
                    )
                )
            for start in range(0, len(documents), BATCH):
                await sink(documents[start : start + BATCH])
            outcome.documents += len(documents)
            if len(rows) < PAGE_SIZE:
                break

    async def fetch_detail(self, client: PoliteClient, external_id: str) -> None:
        raise FetchError(
            "EURES search rows already carry the description; fetch_detail must not be called."
        )


async def _search(client: PoliteClient, codes: list[str], period: str, page: int) -> Any:
    body = {
        "resultsPerPage": PAGE_SIZE,
        "page": page,
        "sortSearch": "MOST_RECENT",
        "publicationPeriod": period,
        "locationCodes": codes,
    }
    return await client.post_json(_SEARCH_URL, json=body, headers={"Accept": "application/json"})


def _total(payload: Any) -> int:
    total = payload.get("numberRecords") if isinstance(payload, dict) else None
    if not isinstance(total, int):
        raise SourceBlocked(
            "EURES returned a body without 'numberRecords', where even an empty search carries it."
        )
    return total


def _regions(payload: Any, country: str) -> list[tuple[str, int]]:
    """A country's NUTS 2 regions and its no-region bucket, with their counts, from the facet."""
    facets = (payload.get("facets") or {}) if isinstance(payload, dict) else {}
    entries = (facets.get("POSITION_LOCATION") or {}).get("facetEntriesList") or []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("code") == country:
            children = entry.get("childrenList") or []
            regions = [
                (str(child["code"]), int(child.get("count") or 0))
                for child in children
                if isinstance(child, dict) and child.get("code")
            ]
            return [(code, count) for code, count in regions if count > 0]
    raise FetchError(
        f"EURES gave no region facet for {country!r}, so a country over the "
        f"{RESULT_WINDOW}-row window cannot be split; sweeping it whole would lose the rest."
    )
