"""The board trial: sweep a discovered board once, keep it if it had a posting a reader can see.

Discovery proposes far more boards than a night can sweep, and most of them hire nowhere near any
reader. The trial is how that pool is turned into a crawl set without a human reading a list:
every candidate is swept once, and a board holding an open posting inside some reader's area is
swept nightly from then on.

This module is only the verdict. Choosing the boards and stamping them tried is in
`ingest/pipeline.py`, where the rest of "which boards does this run sweep" already lives, and
three facts hold its cost down: the trials per run are capped (`settings.trial_boards_per_run`),
a board tried is not tried again for `settings.trial_recheck_days` whichever way the verdict went
(so a 404 costs twelve requests a year, not one a night), and the stamp is written before the
verdict is known.

The verdict cannot be reached in the sweep, because a posting has no facets until the derive queue
has been through it. So `judge` runs after the queues are drained, reads the SAME location filter
a match run reads, and is idempotent: it looks at every candidate's postings, not at the ones a
particular run brought in, so a run that died before its verdict loses nothing.

Nothing here ever switches a board off. A board that had a local posting is the board that will
post the next one, and a crawl set that shrinks on its own shrinks silently -- the same reason a
failure streak does not retire a board either. The only shrinking is an operator's
`trouveur tenants drop`, which `enable_boards_with_local_jobs` is careful not to undo.
"""

from __future__ import annotations

import logging

from trouveur.db.engine import connect
from trouveur.db.queries import coverage as coverage_q
from trouveur.db.queries import discovery as q

log = logging.getLogger(__name__)


def _area_key(area: object) -> tuple:
    """What makes two readers one area here. `['AT','DE']` and `['DE','AT']` are the same filter
    and two stored values, so the lists are sorted into the key or the query runs twice."""
    return (
        tuple(sorted(area.countries or ())),
        tuple(sorted(area.city_ids or ())),
        int(area.radius_km),
        bool(area.remote_anywhere),
    )


async def judge() -> int:
    """Switch on every candidate board with an open posting inside a reader's area.

    One statement per area, because the filter's parameters are per area; a board is worth
    sweeping for everybody as soon as it is worth sweeping for one reader. The profile rows are
    read raw rather than through the coverage report's `Area`, which carries labels and city names
    this has no use for -- and importing it here would make the sweep import the report.
    """
    enabled = 0
    seen: set[tuple] = set()
    async with connect() as conn:
        for area in await coverage_q.areas(conn):
            key = _area_key(area)
            if key in seen:
                continue
            seen.add(key)
            enabled += await q.enable_boards_with_local_jobs(conn, area)
    if enabled:
        log.info("trial enabled %d discovered board(s) for nightly sweeping", enabled)
    return enabled
