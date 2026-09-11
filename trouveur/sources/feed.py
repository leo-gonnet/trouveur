"""Sweeping global sources: one corpus, paged newest-first, no tenant involved.

Workable's public board, Arbeitnow, Himalayas and Jobicy all answer the same shape of question --
"the whole corpus, most recently published first" -- and all of them are far too large to re-fetch
daily. Workable alone reports ~170 000 postings at a fixed 20 per page, which is 8 500 requests
and better part of three hours at the politeness budget.

So the ordinary run is a delta: page until the postings stop being newer than the window, then
stop. That is the cheapest incremental mechanism these sources offer, and using it is what keeps
a daily sweep affordable.

The consequence is the important part. **A delta sweep may close nothing.** It only ever observes
what was published inside its window, so a posting we hold and did not see may be perfectly live,
merely older -- closing on that evidence would retire the entire corpus on the first run. These
sweeps therefore return no closable scope, and lifecycle falls back to closing by age. A backfill
pages to the end and is complete, so it may close; that is the one case where the whole corpus was
actually observed.
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

# A backfill that never terminates is worse than one that stops short, because it holds the whole
# run open. Every source here reports a total, so an honest page budget is derivable, but this is
# the backstop for a source that starts handing out a cursor forever.
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

    `complete_on_backfill` is the source's claim that paging to the end of its cursor chain really
    does observe its entire live set. A source that serves a capped window rather than the whole
    corpus must pass False: reaching the end of one page is not reaching the end of the corpus,
    and closing on it would retire everything the window did not happen to include.
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
            # A cursor that overlaps pages would otherwise archive the same payload twice in one
            # sweep and inflate every count the health panel reports.
            if external_id in seen_ids:
                continue
            posted = published_at(row)
            if cutoff is not None and posted is not None and posted < cutoff:
                # Newest-first ordering means everything after this is older still.
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
        # Only a sweep that actually paged to the end of the corpus observed the whole live set.
        outcome.closable_scopes.append(GLOBAL_SCOPE)
    else:
        # A delta saw only its window, so absence proves nothing; lifecycle closes by age instead.
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
