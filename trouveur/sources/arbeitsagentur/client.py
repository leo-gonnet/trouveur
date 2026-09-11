"""Bundesagentur für Arbeit Jobsuche API — network only, no parsing.

Public and free: no signup, and the API key below is the documented public one. robots.txt is not
applicable (rest.arbeitsagentur.de serves 403 for it); this is a published JSON API intended for
third-party use.

Every constraint in this module was verified against the live API on 2026-09-08, and every one of
them fails *silently* -- zero rows or the entire corpus, never an exception. Re-probe before
changing any of them; do not infer them from the shape of the URL.

  Endpoint versions differ per endpoint and this is not a typo:
    search  pc/v6/jobs                       v6 works; v4 -> 403
    detail  pc/v4/jobdetails/{base64(refnr)} v4 works; v5 and v6 -> 403

  The result list key is `ergebnisliste`, not `stellenangebote`.
  Search results carry no description; only the detail endpoint has one.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

log = logging.getLogger(__name__)

SOURCE = "arbeitsagentur"

_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc"
_SEARCH_URL = f"{_BASE}/v6/jobs"
_DETAIL_URL = f"{_BASE}/v4/jobdetails"

_HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "Accept": "application/json",
    # The API rejects a library-default User-Agent; it wants something browser-shaped.
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
}

# size=500 is the maximum. size=501 returns HTTP 200 with an EMPTY result list rather than an
# error, so an "optimisation" to 1000 would quietly collect nothing at all.
MAX_PAGE_SIZE = 500

# size * page may not exceed 10000; beyond it the API returns HTTP 400. Any partition with more
# matches than this cannot be paged through and must be split or reported as overflowed.
RESULT_WINDOW = 10_000

# veroeffentlichtseit accepts only these values. It is NOT a number of days: 2, 3, 4, 5, 30 and
# 100 are all accepted and all return the ENTIRE ~1M corpus instead of a window, with no error.
# A "2-day catch-up" would therefore silently fetch a million rows. Verified 2026-09-08.
DELTA_WINDOWS = {0, 1, 7, 14}
_DAILY_WINDOW = 1
_BACKFILL_WINDOW = 14


class ArbeitsagenturSource:
    name = SOURCE
    # Descriptions cost one request each, so the sweep records the need and a background worker
    # drains it at a polite constant rate instead of the sweep paying ~36k requests inline.
    requires_detail = True

    def __init__(self, partition_filter: list[str] | None = None) -> None:
        # Restricts the sweep to occupational fields whose name contains one of these strings.
        # For debugging a single partition and for building an evaluation corpus; a production
        # sweep leaves it unset, and a run that sets it logs the narrowing so a corpus that
        # stopped growing is traceable to this rather than to the source.
        self.partition_filter = partition_filter

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        window = _BACKFILL_WINDOW if backfill else _DAILY_WINDOW
        if window not in DELTA_WINDOWS:
            raise FetchError(
                f"veroeffentlichtseit={window} is not one of {sorted(DELTA_WINDOWS)}; the API "
                "would silently return the entire corpus instead of a delta."
            )

        partitions, expected = await self._partitions(client, window)
        if self.partition_filter:
            wanted = [f.lower() for f in self.partition_filter]
            partitions = [p for p in partitions if any(w in p.lower() for w in wanted)]
            log.warning(
                "arbeitsagentur: restricted to %d of the available occupational fields; this is "
                "not a full sweep",
                len(partitions),
            )
        # A delta sweep observes only postings published inside its window, so it can never
        # establish that an older posting has gone. closable_scopes stays empty, which forbids
        # lifecycle from closing anything on the strength of this sweep. See SweepOutcome.
        outcome = SweepOutcome(
            closable_scopes=[], partitions_total=len(partitions), expected=expected
        )

        for label in partitions:
            try:
                seen = await self._sweep_partition(client, sink, label, window, outcome)
            except FetchError as exc:
                outcome.errors.append(f"berufsfeld={label!r}: {exc}")
                log.warning("arbeitsagentur: partition %r failed: %s", label, exc)
                continue
            outcome.documents += seen
            outcome.partitions_done += 1
        return outcome

    async def _partitions(self, client: PoliteClient, window: int) -> tuple[list[str], int]:
        """Read the berufsfeld facet and use its keys as partition boundaries.

        Taken from the API rather than hardcoded so a new occupational field starts being swept on
        its own. The facet counts do not quite sum to the total -- a small share of postings carry
        no berufsfeld and are invisible to a partitioned sweep -- so the caller records the
        shortfall as a measured coverage hole rather than assuming there isn't one.
        """
        payload = await client.get_json(
            _SEARCH_URL, params={"veroeffentlichtseit": window, "size": 1}, headers=_HEADERS
        )
        counts = ((payload.get("facetten") or {}).get("berufsfeld") or {}).get("counts") or {}
        if not counts:
            raise FetchError(
                "Arbeitsagentur returned no berufsfeld facet, so the corpus cannot be "
                "partitioned; sweeping unpartitioned would hit the 10000-row window."
            )
        return sorted(counts), int(payload.get("maxErgebnisse") or 0)

    async def _sweep_partition(
        self,
        client: PoliteClient,
        sink: DocumentSink,
        label: str,
        window: int,
        outcome: SweepOutcome,
    ) -> int:
        seen = 0
        max_page = RESULT_WINDOW // MAX_PAGE_SIZE
        for page in range(1, max_page + 1):
            params = {
                "berufsfeld": label,
                "veroeffentlichtseit": window,
                "size": MAX_PAGE_SIZE,
                "page": page,
            }
            # Deliberately absent: `pav` (HTTP 400), `homeoffice` (HTTP 400) and `arbeitszeit=ho`
            # (0 rows). There is no server-side remote filter; remote is derived from the
            # homeofficemoeglich field instead.
            payload = await client.get_json(_SEARCH_URL, params=params, headers=_HEADERS)
            results = payload.get("ergebnisliste") or []
            if page == 1 and int(payload.get("maxErgebnisse") or 0) > RESULT_WINDOW:
                outcome.partitions_overflowed += 1
                log.warning(
                    "arbeitsagentur: berufsfeld=%r holds %s matches but only %d are reachable; "
                    "results beyond the window are not being collected",
                    label, payload.get("maxErgebnisse"), RESULT_WINDOW,
                )
            documents = [
                RawDocument(
                    source=SOURCE,
                    external_id=str(item["referenznummer"]),
                    kind=DocumentKind.LISTING,
                    payload=item,
                )
                for item in results
                if item.get("referenznummer")
            ]
            if documents:
                await sink(documents)
                seen += len(documents)
            if len(results) < MAX_PAGE_SIZE:
                break
        return seen

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> RawDocument | None:
        token = base64.b64encode(external_id.encode()).decode()
        response = await client.get(f"{_DETAIL_URL}/{token}", headers=_HEADERS)
        # A retired posting 404s. That is the normal end of a listing's life, not a failure.
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise FetchError(
                f"Arbeitsagentur detail for {external_id} returned HTTP {response.status_code}."
            )
        payload: Any = response.json()
        if not isinstance(payload, dict):
            raise FetchError(
                f"Arbeitsagentur detail for {external_id} was not a JSON object."
            )
        return RawDocument(
            source=SOURCE,
            external_id=external_id,
            kind=DocumentKind.DETAIL,
            payload=payload,
        )
