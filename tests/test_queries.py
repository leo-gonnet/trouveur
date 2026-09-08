"""Guards on query shape.

These cannot execute SQL -- the suite runs with no database, by design -- so they assert the
properties of the statements that a reviewer would otherwise have to re-derive by reading them.
Each one corresponds to a change that would compile, run, and be wrong.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from trouveur.db.queries.match import _LEXICAL_SQL, _SEARCH_SQL, recommendations

DIALECT = postgresql.asyncpg.dialect()


def _compiled(sql: str) -> str:
    return str(sa.text(sql).compile(dialect=DIALECT))


def test_like_patterns_survive_compilation():
    """asyncpg uses numeric_dollar paramstyle, so '%%' is NOT unescaped to '%'.

    Doubling them (as psycopg2 requires) leaves two literal percent signs in the query, which
    makes the trigram path search for the character '%' and silently stops matching inside German
    compounds -- the exact recall the index exists to provide, lost with no error.
    """
    for sql in (_LEXICAL_SQL, _SEARCH_SQL):
        assert "%%" not in _compiled(sql)
        assert "LIKE '%' ||" in _compiled(sql)


def test_lexical_retrieval_keeps_both_german_text_paths():
    """Neither path alone is sufficient and removing either loses recall silently.

    The tsvector stems and weights but cannot see 'Ingenieur' inside 'Wirtschaftsingenieur'; the
    unaccented trigram column matches inside compounds and folds umlauts so 'munchen' finds
    'München', but cannot rank.
    """
    assert "websearch_to_tsquery('german'" in _LEXICAL_SQL
    assert "search_fold LIKE" in _LEXICAL_SQL
    assert "f_unaccent" in _LEXICAL_SQL


def test_search_shows_everything_regardless_of_verdict_or_score():
    """Search and Recommendations have deliberately different scope.

    Search is the only view of what was actually collected: a rejected or unscored posting stays
    visible here. Adding a verdict or score filter would turn it into a second, worse
    recommendations page and destroy that.
    """
    where = _SEARCH_SQL.split("WHERE", 1)[1]
    assert "rule_verdict" not in where
    assert "llm_score" not in where
    # It must also be able to show closed postings when asked.
    assert "include_closed" in where


def test_search_left_joins_match_state_so_unmatched_jobs_still_appear():
    # An inner join would silently hide every posting this user has not retrieved, which is most
    # of the corpus, and would look like a source problem rather than a query bug.
    assert re.search(r"LEFT JOIN user_job_match", _SEARCH_SQL)
    assert re.search(r"LEFT JOIN job_facet", _SEARCH_SQL)


def test_recommendations_is_the_strict_page():
    source = recommendations.__doc__ or ""
    assert "strict" in source.lower()


def test_recommendations_requires_a_score_above_the_threshold():
    import inspect

    body = inspect.getsource(recommendations)
    assert "m.rule_verdict = 'pass'" in body
    assert "m.llm_score IS NOT NULL" in body
    assert "m.llm_score >= :threshold" in body
    # Closed postings must never be recommended, whatever they once scored.
    assert "j.closed_at IS NULL" in body


def test_retrieval_filters_exclude_closed_postings():
    from trouveur.db.queries.match import _HARD_FILTERS

    assert "j.closed_at IS NULL" in _HARD_FILTERS


def test_salary_filter_never_rejects_a_posting_that_stated_nothing():
    """A missing salary is not a low salary.

    Rejecting on absence would drop most of the corpus, since the majority of adverts state no
    figure at all.
    """
    from trouveur.db.queries.match import _HARD_FILTERS

    assert "f.salary_max_eur_year IS NULL" in _HARD_FILTERS


def test_empty_profile_filters_match_everything():
    """cardinality(...) = 0 must short-circuit each filter.

    Without it an empty country list would match nothing rather than anything, and a new user
    would see an empty product with no indication why.
    """
    from trouveur.db.queries.match import _HARD_FILTERS

    for field in ("countries", "work_modes", "seniorities", "employment_types"):
        assert f"cardinality(CAST(:{field} AS text[])) = 0" in _HARD_FILTERS
