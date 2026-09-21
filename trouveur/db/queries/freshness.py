"""What counts as fresh, defined once.

A job radar that surfaces a posting a fortnight after it appeared has, for anything competitive,
found nothing. So the corpus a user is matched against is bounded by age, not just by whether the
posting is still open.

This is the one place that bound is written down. It is read by the retrieval filter, by the work
queue that decides what is worth embedding, by the pruner that keeps the ANN index proportional
to the live set, and by the dashboard that reports coverage. Those five disagreeing is not an
error anyone would see: it is an index that grows without bound, or a posting that is embedded
every night and pruned every morning, or a coverage figure that never reaches 100% because its
numerator and denominator count different sets.

Two decisions are encoded here.

**The age of a posting is `COALESCE(posted_at, first_seen_at)`.** Publication date where the
source states one -- it does for every open posting in the corpus today -- and the day we first
saw it where it does not. Not `posted_at` alone: a source that quietly stops sending dates would
have its postings silently dropped from every recommendation, which is precisely the kind of
failure this codebase keeps having to dig out. `first_seen_at` is NOT NULL and is an upper bound
on how long a posting has been in front of us, so falling back to it is conservative rather than
a guess.

**The cutoff is a timestamp computed once per run, not `now()` evaluated per statement.** The
dense arm, the lexical arm and the pruner would otherwise each read a slightly different clock,
so a posting could be fresh for one arm and stale for the other inside a single match -- which
shows up as a result that appears in one ranking and not the other, with nothing to blame.
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
    """The same predicate, for a query built with SQLAlchemy.

    Takes the cutoff rather than a named parameter so that a statement carrying this clause needs
    no extra execute-time arguments -- the queue builds one query for four kinds of work and only
    one of them is bounded by age.
    """
    return sa.func.coalesce(job.c.posted_at, job.c.first_seen_at) > fresh_since
