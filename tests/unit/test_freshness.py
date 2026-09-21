"""The freshness horizon, which five call sites have to agree about.

They disagree silently. An index that grows without bound, a posting embedded every night and
pruned every morning, a coverage figure whose numerator and denominator count different sets --
none of those raise anything. So the two spellings of the predicate are compared here directly.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from trouveur.db.queries import freshness

CUTOFF = datetime(2026, 9, 14, tzinfo=UTC)


def _normalise(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def test_both_spellings_of_the_predicate_say_the_same_thing():
    """One is for queries written as text, one for queries built with SQLAlchemy."""
    built = _normalise(
        str(
            freshness.clause(CUTOFF).compile(
                dialect=sa.dialects.postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
    )
    written = _normalise(freshness.sql("job")).replace(":fresh_since", f"'{CUTOFF}'")
    assert built == written, f"\nSQLAlchemy: {built}\ntext:       {written}"


def test_age_falls_back_to_first_seen_when_a_source_states_no_date():
    """A source that quietly stops sending dates must not vanish from every recommendation."""
    assert "coalesce" in freshness.sql().lower()
    assert "posted_at" in freshness.sql()
    assert "first_seen_at" in freshness.sql()


def test_the_cutoff_is_a_timestamp_not_a_clock_read_per_statement():
    """Both arms of retrieval must agree about which postings are fresh inside one run."""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    assert freshness.fresh_since(7, now=now) == now - timedelta(days=7)
    # Same call, same answer: nothing here reads the clock on its own.
    assert freshness.fresh_since(7, now=now) == freshness.fresh_since(7, now=now)


def test_the_alias_is_honoured_so_a_join_can_use_it():
    assert freshness.sql("j").startswith("COALESCE(j.posted_at")
    assert freshness.sql("job").startswith("COALESCE(job.posted_at")
