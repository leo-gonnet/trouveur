"""Ashby job board API — network only, no parsing.

robots.txt (api.ashbyhq.com, checked 2026-09-09): the host serves no robots.txt at all -- the
request returns "Unauthorized" -- so no restriction is expressed for it. `www.ashbyhq.com` is
`User-agent: * / Allow: /`. Note that `jobs.ashbyhq.com` DOES disallow `/api/`; that is the hosted
HTML board on a different host and is not what this adapter touches. Do not merge the two.

Shape verified live on 2026-09-09 against a real board:

  - one request returns the tenant's COMPLETE live board, so the response is itself the seen-set;
  - `?includeCompensation=true` adds structured salary the free-text summary cannot be parsed
    back out of, so it is always sent;
  - `descriptionPlain` is already plain text, so unlike Greenhouse there is no markup to strip.
"""

from __future__ import annotations

from typing import Any

from trouveur.sources.base import DocumentSink, SweepOutcome
from trouveur.sources.board import sweep_boards
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

SOURCE = "ashby"

_BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{scope}"

# Structured compensation is a separate opt-in. Without it the response carries only a rendered
# summary string ("$211.4K – $290.6K • Offers Equity"), which cannot be turned back into numbers.
_PARAMS = {"includeCompensation": "true"}


class AshbySource:
    name = SOURCE
    requires_detail = False
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
            identify=lambda row: row.get("id"),
            params=_PARAMS,
        )

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> None:
        raise FetchError(
            "Ashby listings already carry the description; fetch_detail must not be called."
        )


def _extract(payload: Any) -> list[dict]:
    rows = (payload or {}).get("jobs") or []
    # `isListed` false means the posting exists but is not published on the board. Archiving it
    # would surface a job nobody can apply to.
    return [row for row in rows if isinstance(row, dict) and row.get("isListed") is not False]
