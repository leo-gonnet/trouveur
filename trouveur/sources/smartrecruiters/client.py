"""SmartRecruiters public Posting API — network only, no parsing.

Probed 2026-10-26, no key:

- `/v1/companies/<id>/postings` lists one company's postings, without their description.
- `limit` above 100 is answered with 100 rows and no error, so the walk asks for exactly 100.
- An unknown company is HTTP 200 with `totalFound: 0`, exactly like a company with no opening:
  a mistyped board reads as empty for ever rather than failing. The resolver's ids come from
  real posting links, which is what keeps that rare.
- The id is matched without regard to case ("AcmeCorp" is "acmecorp"), so a lowercase slug
  addresses it.
- `totalFound` moves between two pages of one walk (1,149 then 1,151), so the walk ends on a short
  page and never on it.
- The description is in `/postings/<id>` (`jobAd.sections`), the detail phase. A posting that is
  gone is HTTP 404 there.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, ScopeResult, SweepOutcome
from trouveur.sources.board import BATCH, scoped_id
from trouveur.sources.errors import FetchError, SourceBlocked
from trouveur.sources.feed import require_list
from trouveur.sources.http import PoliteClient

SOURCE = "smartrecruiters"

_POSTINGS_URL = "https://api.smartrecruiters.com/v1/companies/{scope}/postings"

PAGE_SIZE = 100
# Backstop for a board that never hands out a short page.
MAX_OFFSET = 20_000


class SmartRecruitersSource:
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
        url = _POSTINGS_URL.format(scope=scope)
        seen_ids: set[str] = set()
        for offset in range(0, MAX_OFFSET, PAGE_SIZE):
            response = await client.get(url, params={"limit": PAGE_SIZE, "offset": offset})
            if response.status_code != 200:
                raise FetchError(
                    f"SmartRecruiters board {scope!r} returned HTTP {response.status_code} at "
                    f"offset {offset}."
                )
            try:
                payload: Any = response.json()
            except ValueError as exc:
                raise FetchError(f"SmartRecruiters board {scope!r} did not return JSON.") from exc
            rows = require_list(payload, "content", source=SOURCE)
            fresh = [row for row in rows if (job_id := _id(row)) and job_id not in seen_ids]
            # A page of postings already seen is the offset not advancing, not the end.
            if rows and not fresh:
                return len(seen_ids), False
            seen_ids.update(_id(row) for row in fresh)
            for start in range(0, len(fresh), BATCH):
                await sink(
                    [
                        RawDocument(
                            source=SOURCE,
                            external_id=scoped_id(scope, _id(row)),
                            kind=DocumentKind.LISTING,
                            scope=scope,
                            payload=row,
                        )
                        for row in fresh[start : start + BATCH]
                    ]
                )
            if len(rows) < PAGE_SIZE:
                return len(seen_ids), True
        return len(seen_ids), False

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> RawDocument | None:
        scope, _, posting = external_id.partition(":")
        if not scope or not posting:
            raise FetchError(
                f"SmartRecruiters external id {external_id!r} is not 'company:posting'; it "
                "cannot address a detail request."
            )
        response = await client.get(f"{_POSTINGS_URL.format(scope=scope)}/{posting}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise FetchError(
                f"SmartRecruiters detail for {external_id} returned HTTP {response.status_code}."
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise FetchError(f"SmartRecruiters detail for {external_id} was not JSON.") from exc
        if not isinstance(payload, dict):
            raise FetchError(f"SmartRecruiters detail for {external_id} was not a JSON object.")
        return RawDocument(
            source=SOURCE,
            external_id=external_id,
            kind=DocumentKind.DETAIL,
            scope=scope,
            payload=payload,
        )


def _id(row: dict) -> str | None:
    value = str(row.get("id") or "").strip()
    return value or None
