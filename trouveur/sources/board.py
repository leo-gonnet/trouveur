"""Sweeping sources that publish a tenant's complete board in one request.

Greenhouse, Ashby, Lever, Breezy, Rippling and Personio differ only in the URL they answer on and
the shape of what comes back. Everything else -- looping tenants, isolating one board's failure
from the rest, batching to the sink, recording per-scope health, deciding what may be closed --
is the same, and was the same in each of them before this module existed.

The property that makes them one family is worth stating, because it is what `closable_scopes`
depends on: the response IS the tenant's live set, so a posting we hold in that scope and did not
see is genuinely gone. A source that pages, filters or windows its results is not in this family
and must not be forced into it -- see feed.py.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, ScopeResult, SweepOutcome
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

log = logging.getLogger(__name__)

# Boards are handed to the sink in batches rather than as one multi-megabyte list, so a large
# tenant does not turn into a single oversized transaction.
BATCH = 200

# Extracts the postings from one board response. Returns the rows; anything else about the
# envelope (totals, api version) is the source's business and stays in its own module.
Extract = Callable[[Any], list[dict]]
# The posting's id within its board, as the source states it.
IdentifyFn = Callable[[dict], object]


def scoped_id(scope: str, job_id: object) -> str:
    """Scope a board's own id by tenant.

    Nothing documents any of these platforms' ids as globally unique, and a collision between two
    tenants would silently merge two unrelated postings onto one row.
    """
    return f"{scope}:{job_id}"


async def sweep_boards(
    client: PoliteClient,
    sink: DocumentSink,
    *,
    source: str,
    scopes: Sequence[str],
    url_for: Callable[[str], str],
    extract: Extract,
    identify: IdentifyFn,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    expected_for: Callable[[Any], int | None] | None = None,
    decode: Callable[[httpx.Response], Any] | None = None,
) -> SweepOutcome:
    """Fetch every tenant's complete board once."""
    outcome = SweepOutcome(partitions_total=len(scopes), expected=0)
    for scope in scopes:
        try:
            seen, expected = await _sweep_one(
                client,
                sink,
                source=source,
                scope=scope,
                url=url_for(scope),
                extract=extract,
                identify=identify,
                params=params,
                headers=headers,
                expected_for=expected_for,
                decode=decode,
            )
        except FetchError as exc:
            outcome.errors.append(f"scope={scope}: {exc}")
            outcome.scope_results.append(ScopeResult(scope=scope, ok=False, error=str(exc)))
            log.warning("%s: board %s failed: %s", source, scope, exc)
            continue
        outcome.documents += seen
        outcome.partitions_done += 1
        outcome.expected = (outcome.expected or 0) + (expected if expected is not None else seen)
        outcome.scope_results.append(ScopeResult(scope=scope, ok=True, documents=seen))
        # Recorded per board, not per source: one tenant's board failing tells us nothing about
        # another's, and closing a whole source on a partial sweep would retire every posting
        # belonging to the boards that did not answer.
        outcome.closable_scopes.append(scope)
    return outcome


async def _sweep_one(
    client: PoliteClient,
    sink: DocumentSink,
    *,
    source: str,
    scope: str,
    url: str,
    extract: Extract,
    identify: IdentifyFn,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
    expected_for: Callable[[Any], int | None] | None,
    decode: Callable[[httpx.Response], Any] | None,
) -> tuple[int, int | None]:
    response = await client.get(url, params=params, headers=headers)
    # A retired or renamed board is a registry problem to surface in the UI, not a sweep failure
    # -- but it must not be mistaken for "this board has no jobs" either, which is why it raises
    # rather than returning zero.
    if response.status_code == 404:
        raise FetchError(f"{source} board {scope!r} does not exist (HTTP 404).")
    if response.status_code != 200:
        raise FetchError(f"{source} board {scope!r} returned HTTP {response.status_code}.")

    payload = (decode or _json)(response)
    rows = extract(payload)
    expected = expected_for(payload) if expected_for else None

    seen = 0
    for start in range(0, len(rows), BATCH):
        documents = [
            RawDocument(
                source=source,
                external_id=scoped_id(scope, job_id),
                kind=DocumentKind.LISTING,
                scope=scope,
                payload=row,
            )
            for row in rows[start : start + BATCH]
            if (job_id := identify(row))
        ]
        if documents:
            await sink(documents)
            seen += len(documents)
    return seen, expected


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"{response.url} returned a body that is not JSON: {exc}") from exc
