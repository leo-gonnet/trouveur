"""Workday CXS careers API — network only, no parsing.

robots.txt: checked per tenant host, not per platform, because Workday boards are tenant-hosted
and their rules differ. A representative host on 2026-09-09 allowed the career site path and
disallowed only `/talentcommunity/` and `/refreshFacet/`; `/wday/` was not disallowed. **A new
tenant's robots.txt must be read before it is enabled** -- the sibling SuccessFactors platform was
observed serving `Disallow: /` on one tenant host and nothing on another, so a platform-wide
verdict is not sound for this family.

Shape verified live on 2026-09-09. Three behaviours here fail silently and all three are guarded:

  - **`limit` caps at exactly 20.** `limit=21`, `50` and `100` all return HTTP 400. Raising the
    page size "for throughput" fails the whole sweep.
  - **`total` is not usable as a count or as a terminator.** One query returned total=2000 at
    offset 0, total=0 at offset 1980, and total=2000 at offset 2000 -- while still returning rows
    past its own stated total. Paging until `offset >= total` would stop early or never stop.
    The only reliable end is an empty `jobPostings` array.
  - **`postedOn` is relative prose** ("Posted Today", "Posted 30+ Days Ago"), not a date. It is
    archived as stated and resolved in derivation, never parsed into an absolute here.

The listing carries no description, so every posting costs one detail request.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, ScopeResult, SweepOutcome
from trouveur.sources.board import BATCH, scoped_id
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient
from trouveur.sources.scopes import split_workday_scope

SOURCE = "workday"

_JOBS_URL = "https://{tenant}.{instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

# Verified live: 21 and above return HTTP 400. This is a hard ceiling, not a default.
PAGE_SIZE = 20

# A tenant with more postings than this is paged past rather than swept forever. Recorded as an
# overflow so a growing number is visible, in the same spirit as the Arbeitsagentur result window.
MAX_OFFSET = 10_000


class WorkdaySource:
    name = SOURCE
    # The listing has no description; every posting needs a detail fetch.
    requires_detail = True
    tenant_scoped = True

    def __init__(self, boards: list[str] | None = None) -> None:
        self.boards = boards or []

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        # Paging a tenant to exhaustion observes its whole live set, so a backfill and a daily run
        # are the same walk. The flag is accepted for protocol conformance and deliberately unused.
        outcome = SweepOutcome(partitions_total=len(self.boards), expected=0)
        for scope in self.boards:
            try:
                seen, overflowed = await self._sweep_board(client, sink, scope)
            except FetchError as exc:
                outcome.errors.append(f"scope={scope}: {exc}")
                outcome.scope_results.append(ScopeResult(scope=scope, ok=False, error=str(exc)))
                continue
            outcome.documents += seen
            outcome.partitions_done += 1
            outcome.expected = (outcome.expected or 0) + seen
            outcome.scope_results.append(ScopeResult(scope=scope, ok=True, documents=seen))
            if overflowed:
                outcome.partitions_overflowed += 1
                # The sweep did not reach the end of this board, so postings it did not see may
                # still be live and nothing here may be closed.
                continue
            outcome.closable_scopes.append(scope)
        return outcome

    async def _sweep_board(
        self, client: PoliteClient, sink: DocumentSink, scope: str
    ) -> tuple[int, bool]:
        tenant, instance, site = split_workday_scope(scope)
        url = _JOBS_URL.format(tenant=tenant, instance=instance, site=site)

        seen = 0
        offset = 0
        while offset < MAX_OFFSET:
            payload = await self._page(client, url, scope, offset)
            rows = payload.get("jobPostings")
            rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            # The only reliable end of the walk. `total` contradicts itself across pages.
            if not rows:
                return seen, False

            for start in range(0, len(rows), BATCH):
                documents = [
                    RawDocument(
                        source=SOURCE,
                        external_id=scoped_id(scope, path),
                        kind=DocumentKind.LISTING,
                        scope=scope,
                        payload=row,
                    )
                    for row in rows[start : start + BATCH]
                    if (path := _external_path(row))
                ]
                if documents:
                    await sink(documents)
                    seen += len(documents)
            offset += PAGE_SIZE
        return seen, True

    async def _page(
        self, client: PoliteClient, url: str, scope: str, offset: int
    ) -> dict:
        response = await client.post(
            url,
            json={"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""},
            headers={"Accept": "application/json"},
        )
        if response.status_code == 404:
            raise FetchError(f"Workday board {scope!r} does not exist (HTTP 404).")
        if response.status_code != 200:
            raise FetchError(
                f"Workday board {scope!r} returned HTTP {response.status_code} at offset "
                f"{offset}."
            )
        payload: Any = response.json()
        if not isinstance(payload, dict):
            raise FetchError(f"Workday board {scope!r} did not return a JSON object.")
        return payload

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> RawDocument | None:
        scope, _, path = external_id.partition(":")
        if not scope or not path:
            raise FetchError(
                f"Workday external id {external_id!r} is not 'scope:externalPath'; it cannot "
                "address a detail request."
            )
        tenant, instance, site = split_workday_scope(scope)
        base = _JOBS_URL.format(tenant=tenant, instance=instance, site=site).removesuffix("/jobs")
        response = await client.get(f"{base}{path}")
        # A retired posting 404s. That is the normal end of a listing's life, not a failure.
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise FetchError(
                f"Workday detail for {external_id} returned HTTP {response.status_code}."
            )
        payload: Any = response.json()
        if not isinstance(payload, dict):
            raise FetchError(f"Workday detail for {external_id} was not a JSON object.")
        return RawDocument(
            source=SOURCE,
            external_id=external_id,
            kind=DocumentKind.DETAIL,
            scope=scope,
            payload=payload,
        )


def _external_path(row: dict) -> str | None:
    """Workday's own per-posting path, which is both its id and its detail address."""
    path = row.get("externalPath")
    return path if isinstance(path, str) and path.strip() else None
