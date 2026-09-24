"""Workday CXS careers API — network only, no parsing.

robots.txt is checked PER TENANT HOST, not per platform: boards are tenant-hosted and their rules
differ. A representative host on 2026-09-09 disallowed only `/talentcommunity/` and
`/refreshFacet/`. A new tenant's robots.txt must be read before it is enabled.

`limit` caps at exactly 20, `total` contradicts itself across pages, and `postedOn` is relative
prose. Each is guarded below.
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

# Verified live: 21 and above return HTTP 400. A hard ceiling, not a default.
PAGE_SIZE = 20

MAX_OFFSET = 10_000


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
                # The walk did not reach the end, so nothing here may be closed.
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
        seen_paths: set[str] = set()
        while offset < MAX_OFFSET:
            payload = await self._page(client, url, scope, offset)
            rows = payload.get("jobPostings")
            rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            # The only reliable end of the walk. `total` contradicts itself across pages.
            if not rows:
                return seen, False

            # Past its last posting Workday CLAMPS the offset rather than running out (probed
            # 2026-09-22: offset=2000 and offset=9980 returned the identical page), so the empty
            # page above never arrives for most boards. A page carrying nothing new is that clamp
            # and is a real end -- everything behind it has already been sinked.
            paths = {path for row in rows if (path := _external_path(row))}
            if paths and paths <= seen_paths:
                return seen, False
            seen_paths |= paths

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
