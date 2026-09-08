"""Greenhouse job boards API — network only, no parsing.

robots.txt (boards-api.greenhouse.io, checked 2026-09-08): the only rule is `Disallow: /embed/`,
so the `/v1/boards/` JSON API used here is explicitly permitted. No authentication is required.

Shape verified live on 2026-09-08 and deliberately opposite to Arbeitsagentur, which is why these
two were chosen together:

  - one request returns a tenant's COMPLETE live board, with `meta.total` and no pagination, so
    the response is itself the seen-set and closing is exact rather than inferred;
  - `?content=true` includes the full description, so there is no detail phase at all;
  - there is no index of tenants anywhere, so the board registry in the database is the only list
    of boards that exists. An unknown slug returns HTTP 404.
"""

from __future__ import annotations

import logging

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

log = logging.getLogger(__name__)

SOURCE = "greenhouse"

_BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

# A board's jobs are handed to the sink in batches rather than as one 3-4MB list, so a large
# tenant does not turn into a single oversized transaction.
_BATCH = 200


def external_id(slug: str, job_id: object) -> str:
    """Scope the board's own id by tenant.

    Greenhouse ids appear to be globally unique, but nothing documents that, and a collision
    between two tenants would silently merge two unrelated postings onto one row.
    """
    return f"{slug}:{job_id}"


class GreenhouseSource:
    name = SOURCE
    # The listing already carries the description, so no second request is ever needed.
    requires_detail = False

    def __init__(self, boards: list[str]) -> None:
        # Only ever sees the boards stored in the database; there is no global tenant index.
        self.boards = boards

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        # A board dump is always the complete live set, so a backfill and a daily run are the
        # same request. The flag is accepted for protocol conformance and deliberately unused.
        outcome = SweepOutcome(partitions_total=len(self.boards), expected=0)
        for slug in self.boards:
            try:
                seen = await self._sweep_board(client, sink, slug, outcome)
            except FetchError as exc:
                outcome.errors.append(f"board={slug}: {exc}")
                log.warning("greenhouse: board %s failed: %s", slug, exc)
                continue
            outcome.documents += seen
            outcome.partitions_done += 1
            # Recorded per board, not per source: one tenant's board failing tells us nothing
            # about another's, and closing a whole source on a partial sweep would retire every
            # posting belonging to the boards that did not answer.
            outcome.closable_scopes.append(slug)
        return outcome

    async def _sweep_board(
        self, client: PoliteClient, sink: DocumentSink, slug: str, outcome: SweepOutcome
    ) -> int:
        response = await client.get(
            _BOARD_URL.format(slug=slug), params={"content": "true"}
        )
        # A retired or renamed board 404s. That is a registry problem to surface in the UI, not a
        # sweep failure, but it must not be mistaken for "this board has no jobs" either.
        if response.status_code == 404:
            raise FetchError(f"Greenhouse board {slug!r} does not exist (HTTP 404).")
        if response.status_code != 200:
            raise FetchError(
                f"Greenhouse board {slug!r} returned HTTP {response.status_code}."
            )
        payload = response.json()
        jobs = payload.get("jobs") or []
        outcome.expected = (outcome.expected or 0) + int(
            (payload.get("meta") or {}).get("total") or len(jobs)
        )

        seen = 0
        for start in range(0, len(jobs), _BATCH):
            documents = [
                RawDocument(
                    source=SOURCE,
                    external_id=external_id(slug, job["id"]),
                    kind=DocumentKind.LISTING,
                    payload=job,
                )
                for job in jobs[start : start + _BATCH]
                if job.get("id")
            ]
            if documents:
                await sink(documents)
                seen += len(documents)
        return seen

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> RawDocument | None:
        raise FetchError(
            "Greenhouse listings already carry the description; fetch_detail must not be called."
        )
