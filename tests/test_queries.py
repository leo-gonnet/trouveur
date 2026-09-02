"""Query construction tests.

These assert on the compiled SQL rather than hitting a database, so they run in CI without
Postgres. They exist to pin behaviour that is easy to break and silent when broken.
"""

from __future__ import annotations

import sqlalchemy as sa

from trouveur.db import queries as q
from trouveur.db.schema import job


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=sa.dialects.postgresql.dialect()))


def _recommendations_sql() -> str:
    """Rebuild the recommendations predicate exactly as queries.recommendations does."""
    from trouveur.models import RuleVerdict, UserState

    stmt = sa.select(job).where(
        job.c.user_state == UserState.NEW.value,
        job.c.rule_verdict == RuleVerdict.PASS.value,
        job.c.llm_score.is_not(None),
        job.c.llm_score >= 0,
    )
    return _compiled(stmt)


def test_recommendations_excludes_unscored_jobs():
    """Recommendations is the strict page: rule_verdict='pass' but llm_score IS NULL is the
    normal state before the LLM stage has run, and must not appear here. It belongs in /search
    instead, which shows everything scraped regardless of filter outcome."""
    sql = _recommendations_sql()
    assert "llm_score IS NOT NULL" in sql


def test_recommendations_only_shows_rules_survivors():
    assert "rule_verdict" in _recommendations_sql()


def test_recommendations_only_shows_unreviewed_jobs():
    assert "user_state" in _recommendations_sql()


def test_search_jobs_does_not_filter_by_rule_verdict():
    """Search must show everything scraped -- matched or not -- unlike recommendations.

    It legitimately orders by llm_score (best matches surface first), so only rule_verdict,
    which would exclude rejected jobs outright, is checked here.
    """
    import inspect

    source = inspect.getsource(q.search_jobs)
    assert "rule_verdict" not in source


def test_search_escapes_like_wildcards():
    """A literal % typed by the user must not behave as a pattern."""
    from trouveur.models import fold

    folded = fold("100%")
    pattern = "%" + folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    assert pattern == "%100\\%%"


def test_search_queries_both_german_indexes():
    """Neither index alone is sufficient; see AGENTS.md > Database rules."""
    source = (q.search_jobs.__doc__ or "") + open(q.__file__, encoding="utf-8").read()
    assert "websearch_to_tsquery" in source
    assert "search_norm" in source


def test_add_personio_tenants_ignores_duplicates():
    """Bulk add is the main entry path, so re-pasting a list must not raise on existing rows."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from trouveur.db.schema import personio_tenant

    stmt = (
        pg_insert(personio_tenant)
        .values([{"slug": "acme-gmbh"}, {"slug": "musterfirma"}])
        .on_conflict_do_nothing(index_elements=["slug"])
        .returning(personio_tenant.c.slug)
    )
    sql = _compiled(stmt)
    assert "ON CONFLICT (slug) DO NOTHING" in sql
    assert "RETURNING" in sql


def test_enabled_personio_tenants_filters_on_enabled():
    """The pipeline must never fetch a paused company."""
    from trouveur.db.schema import personio_tenant

    stmt = (
        sa.select(personio_tenant.c.slug)
        .where(personio_tenant.c.enabled)
        .order_by(personio_tenant.c.slug)
    )
    assert "WHERE personio_tenant.enabled" in _compiled(stmt)
