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
    server_side_window: bool = False,
) -> SweepOutcome:
    """Page a global feed, newest first, stopping at the delta window unless backfilling.

    A source serving a capped window rather than the whole corpus must pass
    `complete_on_backfill=False`: the end of its one page is not the end of the corpus.

    `server_side_window` says the source narrowed the window in the REQUEST, so the walk runs to
    the end of what the server returned and the client-side cutoff is not applied. It is for a
    feed that is not strictly newest-first -- Workable leads with boosted postings ordered by when
    their boost expires -- where the first out-of-window row says nothing about the rows after it
    and breaking on it drops most of the day.
    """
    outcome = SweepOutcome(partitions_total=1)
    cutoff = None if backfill or server_side_window else datetime.now(UTC) - delta_window
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

        added = 0
        in_window = 0
        for row in rows:
            job_id = identify(row)
            if not job_id:
                continue
            external_id = str(job_id)
            if external_id in seen_ids:
                continue
            added += 1
            seen_ids.add(external_id)
            posted = published_at(row)
            # Skipped, not stopped on: Arbeitnow pins a weeks-old posting above today's, and
            # breaking on it collected nothing at all on 2026-09-29 while reporting success.
            if cutoff is not None and posted is not None and posted < cutoff:
                continue
            in_window += 1
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

        # A whole page older than the window is its edge: at most one page past it, never years.
        if cutoff is not None and added and not in_window:
            reached_end = True
            break
        # A full page of rows we already hold means the cursor is not advancing, whatever it
        # says. Workable's did exactly that -- it ignores an unknown page parameter and serves
        # page one for ever -- and with every row deduplicated and none of them old enough to
        # exhaust the window, the walk ran to MAX_PAGES: 20,000 requests at one provider, which
        # is how the sweep came to fail on an HTTP 429 rather than on the paging bug itself.
        # NOT an end of corpus: `reached_end` stays false, so a backfill cannot close on it.
        if not added:
            log.warning(
                "%s: page %d added nothing new; the cursor is not advancing, stopping",
                source, _page + 1,
            )
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
