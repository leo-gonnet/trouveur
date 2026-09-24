"""Sweeping sources that publish a tenant's complete board in one request.

What makes them one family is what `closable_scopes` depends on: the response IS the tenant's
live set. A source that pages, filters or windows its results is not in this family -- see
feed.py.
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

BATCH = 200

Extract = Callable[[Any], list[dict]]
IdentifyFn = Callable[[dict], object]


def scoped_id(scope: str, job_id: object) -> str:
    """Scope a board's own id by tenant. None of these platforms documents its ids as globally
    unique, and a collision would silently merge two unrelated postings onto one row."""
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
        # Per board, not per source: one tenant's board failing says nothing about another's.
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
    # Raises rather than returning zero: a dead board must not read as "this board has no jobs".
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
