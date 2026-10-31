"""Workday CXS careers API — network only, no parsing.

robots.txt is checked PER TENANT HOST, not per platform: boards are tenant-hosted and their rules
differ. A representative host on 2026-09-09 disallowed only `/talentcommunity/` and
`/refreshFacet/`. A new tenant's robots.txt must be read before it is enabled.

`limit` caps at exactly 20, `total` contradicts itself across pages, some tenants cap a query at
2,000 postings, and `postedOn` is relative prose. Each is guarded below.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Collection, Iterator
from typing import Any

import httpx

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, ScopeResult, SweepOutcome
from trouveur.sources.board import BATCH, scoped_id
from trouveur.sources.errors import SOURCE_BURST, FetchError, SourceBlocked
from trouveur.sources.http import PoliteClient, is_challenge
from trouveur.sources.scopes import split_workday_scope

log = logging.getLogger(__name__)

SOURCE = "workday"

_JOBS_URL = "https://{tenant}.{instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

# Verified live: 21 and above return HTTP 400. A hard ceiling, not a default.
PAGE_SIZE = 20

MAX_OFFSET = 10_000

# Some tenants cap a query at 2,000 postings: page one reports exactly this total, and past it the
# walk wraps to the first page, which reads as the end of the board. Probed 2026-10-31: nine boards
# stopped at 2,000 while their facet counts summed to 2,050-4,100. Such a query is split by facet
# until every slice is under the cap.
CAPPED_TOTAL = 2000
MAX_SPLIT_FACETS = 3

# Each board has its own budget (`http._BUDGET_PER_HOST`), so this only bounds the total in
# flight: 146 tenants together took 64 req/s without a refusal.
BOARDS_AT_ONCE = 8


class WorkdaySource:
    name = SOURCE
    requires_detail = True
    tenant_scoped = True

    def __init__(self, boards: list[str] | None = None) -> None:
        self.boards = boards or []

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        outcome = SweepOutcome(partitions_total=len(self.boards), expected=0)
        gate = asyncio.Semaphore(BOARDS_AT_ONCE)
        blocked: list[str] = []

        async def sweep_one(scope: str) -> None:
            async with gate:
                if len(blocked) >= SOURCE_BURST:
                    return
                try:
                    seen, overflowed = await self._sweep_board(client, sink, scope)
                # Before FetchError, which it is. Workday limits each company on its own, so one
                # company refusing stops that company; several mean Workday refuses us.
                except SourceBlocked as exc:
                    blocked.append(scope)
                    outcome.errors.append(f"scope={scope}: {exc}")
                    outcome.scope_results.append(
                        ScopeResult(scope=scope, ok=False, error=str(exc), blocked=True)
                    )
                    if len(blocked) >= SOURCE_BURST:
                        raise
                    return
                except FetchError as exc:
                    outcome.errors.append(f"scope={scope}: {exc}")
                    outcome.scope_results.append(
                        ScopeResult(scope=scope, ok=False, error=str(exc))
                    )
                    return
            outcome.documents += seen
            outcome.partitions_done += 1
            outcome.expected = (outcome.expected or 0) + seen
            outcome.scope_results.append(ScopeResult(scope=scope, ok=True, documents=seen))
            if overflowed:
                outcome.partitions_overflowed += 1
                # The walk did not reach the end, so nothing here may be closed.
                return
            outcome.closable_scopes.append(scope)

        try:
            async with asyncio.TaskGroup() as group:
                for scope in self.boards:
                    group.create_task(sweep_one(scope))
        except ExceptionGroup as failed:
            # The group has already cancelled the boards still walking.
            raise failed.exceptions[0] from None
        return outcome

    async def _sweep_board(
        self, client: PoliteClient, sink: DocumentSink, scope: str
    ) -> tuple[int, bool]:
        tenant, instance, site = split_workday_scope(scope)
        url = _JOBS_URL.format(tenant=tenant, instance=instance, site=site)
        first = await self._page(client, url, scope, 0, {})
        sunk: set[str] = set()
        if first.get("total") == CAPPED_TOTAL:
            return await self._split(client, sink, scope, url, {}, first, sunk)
        return await self._walk(client, sink, scope, url, {}, first, sunk)

    async def _split(
        self,
        client: PoliteClient,
        sink: DocumentSink,
        scope: str,
        url: str,
        facets: dict[str, list[str]],
        capped: dict,
        sunk: set[str],
    ) -> tuple[int, bool]:
        dimension = _split_dimension(capped.get("facets"), facets.keys())
        if dimension is None or len(facets) >= MAX_SPLIT_FACETS:
            log.warning(
                "Workday board %s holds more than %d postings under %s and no facet splits it "
                "further; its postings stay open.",
                scope, CAPPED_TOTAL, facets or "no filter",
            )
            seen, _ = await self._walk(client, sink, scope, url, facets, capped, sunk)
            return seen, True
        parameter, values, covers = dimension
        if not covers:
            log.warning(
                "Workday board %s: no two facets agree on its size under %s, so splitting on %s "
                "cannot be proven complete; its postings stay open.",
                scope, facets or "no filter", parameter,
            )
        seen, overflowed = 0, not covers
        for value in values:
            sliced = {**facets, parameter: [value]}
            first = await self._page(client, url, scope, 0, sliced)
            if first.get("total") == CAPPED_TOTAL:
                count, over = await self._split(client, sink, scope, url, sliced, first, sunk)
            else:
                count, over = await self._walk(client, sink, scope, url, sliced, first, sunk)
            seen += count
            overflowed = overflowed or over
        return seen, overflowed

    async def _walk(
        self,
        client: PoliteClient,
        sink: DocumentSink,
        scope: str,
        url: str,
        facets: dict[str, list[str]],
        first: dict,
        sunk: set[str],
    ) -> tuple[int, bool]:
        """Page one query to its end. `sunk` is shared by every slice of the board, since a
        posting can sit in several values of one facet (two locations)."""
        seen = 0
        offset = 0
        walked: set[str] = set()
        payload = first
        while True:
            rows = payload.get("jobPostings")
            rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            # The only reliable end of the walk. `total` contradicts itself across pages.
            if not rows:
                return seen, False

            # Past its last posting Workday CLAMPS the offset rather than running out (probed
            # 2026-09-22: offset=2000 and offset=9980 returned the identical page), so the empty
            # page above never arrives for most boards. A page carrying nothing new is that clamp
            # and is a real end -- everything behind it has already been sinked. A capped query
            # wraps the same way at CAPPED_TOTAL, which is why it is split before it gets here.
            paths = {path for row in rows if (path := _external_path(row))}
            if paths and paths <= walked:
                return seen, False
            walked |= paths

            fresh = [row for row in rows if (path := _external_path(row)) and path not in sunk]
            sunk |= paths
            for start in range(0, len(fresh), BATCH):
                documents = [
                    RawDocument(
                        source=SOURCE,
                        external_id=scoped_id(scope, _external_path(row)),
                        kind=DocumentKind.LISTING,
                        scope=scope,
                        payload=row,
                    )
                    for row in fresh[start : start + BATCH]
                ]
                await sink(documents)
                seen += len(documents)
            offset += PAGE_SIZE
            if offset >= MAX_OFFSET:
                return seen, True
            payload = await self._page(client, url, scope, offset, facets)

    async def _page(
        self,
        client: PoliteClient,
        url: str,
        scope: str,
        offset: int,
        facets: dict[str, list[str]],
    ) -> dict:
        response = await client.post(
            url,
            json={
                "appliedFacets": facets,
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": "",
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code == 404:
            raise FetchError(f"Workday board {scope!r} does not exist (HTTP 404).")
        if response.status_code != 200:
            raise FetchError(
                f"Workday board {scope!r} returned HTTP {response.status_code} at offset "
                f"{offset}."
            )
        payload = _json(response, f"Workday board {scope!r} at offset {offset}")
        if not isinstance(payload, dict):
            raise FetchError(f"Workday board {scope!r} did not return a JSON object.")
        return payload

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> RawDocument | None:
        # The id is `tenant:instance:site:/job/...` -- four fields. Splitting on the first colon
        # yields the tenant alone and raises a ValueError `drain_detail` does not catch.
        tenant, _, rest = external_id.partition(":")
        instance, _, rest = rest.partition(":")
        site, _, path = rest.partition(":")
        if not (tenant and instance and site and path):
            raise FetchError(
                f"Workday external id {external_id!r} is not 'tenant:instance:site:externalPath'; "
                "it cannot address a detail request."
            )
        scope = f"{tenant}:{instance}:{site}"
        base = _JOBS_URL.format(tenant=tenant, instance=instance, site=site).removesuffix("/jobs")
        response = await client.get(f"{base}{path}")
        if response.status_code == 404 or _withdrawn(response):
            return None
        if response.status_code != 200:
            raise FetchError(
                f"Workday detail for {external_id} returned HTTP {response.status_code}."
            )
        payload = _json(response, f"Workday detail for {external_id}")
        if not isinstance(payload, dict):
            raise FetchError(f"Workday detail for {external_id} was not a JSON object.")
        return RawDocument(
            source=SOURCE,
            external_id=external_id,
            kind=DocumentKind.DETAIL,
            scope=scope,
            payload=payload,
        )


def _json(response: httpx.Response, what: str) -> Any:
    """A 200 whose body is not JSON came back mid-sweep on 2026-10-23 and 2026-10-31, at 1 req/s,
    and its ValueError ended the whole source: 173 boards, none closable. It fails one board,
    unless it is a bot challenge, which `PoliteClient` only recognises on a refusal status."""
    if is_challenge(response):
        raise SourceBlocked(f"{what} answered HTTP 200 with a bot challenge page.")
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"{what} answered HTTP 200 with a body that is not JSON.") from exc


def _withdrawn(response: httpx.Response) -> bool:
    """Workday answers a posting taken down after we listed it with 403 and error code S22, not
    404. Probed 2026-10-31: 57 of 59 such postings were gone from their board's search, where 12
    of 12 live controls were found. Some tenants also answer a burst with 403, so a 403 without
    S22 stays a failure; a posting closed by mistake reopens when the next sweep lists it."""
    if response.status_code != 403:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("errorCode") == "S22"


def _external_path(row: dict) -> str | None:
    """Workday's own per-posting path, which is both its id and its detail address."""
    path = row.get("externalPath")
    return path if isinstance(path, str) and path.strip() else None


def _split_dimension(
    facets: Any, used: Collection[str]
) -> tuple[str, list[str], bool] | None:
    """The facet to split a capped query on, and whether it provably covers the whole query.

    No facet states the true total, and a facet's sum misleads both ways: a location facet counts
    a posting once per place, and Workday truncates others (a 55-value `hiringCompany` summed to
    2,717 on a board of 3,208). Two facets summing to the same figure are taken as that total,
    and a facet reaching it covers every posting -- on the capped boards of 2026-10-31 the
    location facets came up 18 and 63 postings short of it. With no such figure the widest facet
    is walked for recall, but the walk cannot be called complete.

    Then the cheapest, since every value is a request: among facets clearing the cap in one level
    the fewest values, otherwise the smallest largest value, the likeliest to clear it one level
    down. A facet summing below the cap cannot partition the query at all."""
    candidates = [
        (parameter, counts)
        for parameter, counts in _flat_facets(facets)
        if parameter not in used and sum(counts.values()) >= CAPPED_TOTAL
    ]
    if not candidates:
        return None
    sums = Counter(sum(counts.values()) for _parameter, counts in candidates)
    agreed = max((total for total, seen in sums.items() if seen >= 2), default=None)
    target = agreed if agreed is not None else max(sums)

    def cost(counts: dict[str, int]) -> tuple[bool, int]:
        largest = max(counts.values())
        clears = largest < CAPPED_TOTAL
        return (not clears, len(counts) if clears else largest)

    parameter, counts = min(
        (item for item in candidates if sum(item[1].values()) == target),
        key=lambda item: cost(item[1]),
    )
    values = [value for value, count in counts.items() if count > 0]
    return parameter, values, agreed is not None


def _flat_facets(facets: Any) -> Iterator[tuple[str, dict[str, int]]]:
    """Every facet a query can be scoped by. A wrapper such as `locationMainGroup` holds facets
    rather than values; applied as itself it filters nothing, so its children are used instead."""
    if not isinstance(facets, list):
        return
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        values = facet.get("values") if isinstance(facet.get("values"), list) else []
        counts: dict[str, int] = {}
        nested = []
        for value in values:
            if not isinstance(value, dict):
                continue
            if value.get("facetParameter"):
                nested.append(value)
            elif isinstance(value.get("id"), str) and isinstance(value.get("count"), int):
                counts[value["id"]] = value["count"]
        yield from _flat_facets(nested)
        parameter = facet.get("facetParameter")
        if isinstance(parameter, str) and counts:
            yield parameter, counts
