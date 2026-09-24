"""Sweeping global sources: one corpus, paged newest-first, no tenant involved.

The ordinary run is a delta and therefore CLOSES NOTHING: it observes only what was published
inside its window, so absence proves nothing and closing on it would retire the whole corpus on
the first run. Only a backfill that pages to the end may close.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import GLOBAL_SCOPE, DocumentSink, ScopeResult, SweepOutcome
from trouveur.sources.errors import FetchError

log = logging.getLogger(__name__)

BATCH = 200

# Backstop for a source that starts handing out a cursor forever.
MAX_PAGES = 20_000


async def sweep_feed(
    sink: DocumentSink,
    *,
    source: str,
    fetch: Callable[[object | None], Any],
    extract: Callable[[Any], list[dict]],
    next_cursor: Callable[[Any], object | None],
    identify: Callable[[dict], object],
    published_at: Callable[[dict], datetime | None],
    backfill: bool,
    delta_window: timedelta,
    expected_for: Callable[[Any], int | None] | None = None,
    complete_on_backfill: bool = True,
) -> SweepOutcome:
    """Page a global feed, newest first, stopping at the delta window unless backfilling.

    A source serving a capped window rather than the whole corpus must pass
    `complete_on_backfill=False`: the end of its one page is not the end of the corpus.
    """
    outcome = SweepOutcome(partitions_total=1)
    cutoff = None if backfill else datetime.now(UTC) - delta_window
    cursor: object | None = None
    pending: list[RawDocument] = []
    seen_ids: set[str] = set()
    reached_end = False

    for _page in range(MAX_PAGES):
        payload = await fetch(cursor)
        if outcome.expected is None and expected_for:
            outcome.expected = expected_for(payload)
        rows = extract(payload)
        if not rows:
            reached_end = True
            break

        exhausted = False
        for row in rows:
            job_id = identify(row)
            if not job_id:
                continue
            external_id = str(job_id)
            if external_id in seen_ids:
                continue
            posted = published_at(row)
            if cutoff is not None and posted is not None and posted < cutoff:
                exhausted = True
                break
            seen_ids.add(external_id)
            pending.append(
                RawDocument(
                    source=source,
                    external_id=external_id,
                    kind=DocumentKind.LISTING,
                    scope=GLOBAL_SCOPE,
                    payload=row,
                )
            )

        while len(pending) >= BATCH:
            await sink(pending[:BATCH])
            outcome.documents += BATCH
            del pending[:BATCH]

        if exhausted:
            reached_end = True
            break
        cursor = next_cursor(payload)
        if cursor is None:
            reached_end = True
            break
    else:
        log.warning("%s: stopped after %d pages without reaching the end", source, MAX_PAGES)

    if pending:
        await sink(pending)
        outcome.documents += len(pending)

    outcome.partitions_done = 1 if reached_end else 0
    outcome.scope_results.append(
        ScopeResult(scope=GLOBAL_SCOPE, ok=reached_end, documents=outcome.documents)
    )
    if backfill and reached_end and complete_on_backfill:
        outcome.closable_scopes.append(GLOBAL_SCOPE)
    else:
        outcome.expected = None
    return outcome


def page_params(base: dict[str, Any] | None, extra: dict[str, Any]) -> dict[str, Any]:
    """Merge per-page params onto a source's fixed ones without mutating the source's dict."""
    merged = dict(base or {})
    merged.update(extra)
    return merged


def require_list(payload: Any, key: str | None, *, source: str) -> list[dict]:
    """The rows of a response, or a complete sentence about why they are not there."""
    rows = payload if key is None else (payload or {}).get(key)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise FetchError(
            f"{source} returned {key or 'a body'} that is not a list but "
            f"{type(rows).__name__}; the response shape has changed."
        )
    return [row for row in rows if isinstance(row, dict)]


__all__ = ["BATCH", "MAX_PAGES", "page_params", "require_list", "sweep_feed"]
