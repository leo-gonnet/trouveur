"""What counts as fresh, defined once. Read by both retrieval arms, the embed queue, the pruner
and the dashboard; the five disagreeing produces an index that grows without bound, or a posting
embedded every night and pruned every morning.

Age is `COALESCE(posted_at, first_seen_at)`, never `posted_at` alone: a source that quietly
stopped sending dates would drop out of every recommendation. The cutoff is a timestamp computed
once per run, never `now()` per statement, or one arm can call a posting fresh and another stale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from trouveur.db.schema import job


def fresh_since(horizon_days: int, *, now: datetime | None = None) -> datetime:
    """The oldest publication date still eligible. Pass this as `:fresh_since`."""
    return (now or datetime.now(UTC)) - timedelta(days=horizon_days)


def sql(alias: str = "j") -> str:
    """The freshness predicate, for a query written as text."""
    return f"COALESCE({alias}.posted_at, {alias}.first_seen_at) > :fresh_since"


def clause(fresh_since: datetime):
    """The same predicate, for a query built with SQLAlchemy. Takes the cutoff rather than a
    named parameter, so a statement carrying it needs no extra execute-time arguments."""
    return sa.func.coalesce(job.c.posted_at, job.c.first_seen_at) > fresh_since
