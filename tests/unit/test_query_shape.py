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


def test_recommendations_shows_everything_that_was_scored():
    """Still narrower than Search -- rule-passed and scored -- but with no cut-off inside that.

    A threshold hid postings the user had already paid to have scored, behind a number they had
    to guess. Re-adding one would do it again, silently.
    """
    import inspect

    body = inspect.getsource(recommendations)
    assert "m.rule_verdict = 'pass'" in body
    assert "m.llm_score IS NOT NULL" in body
    # Closed postings must never be recommended, whatever they once scored.
    assert "j.closed_at IS NULL" in body
    assert ":threshold" not in body
    assert "llm_score >=" not in body


def test_recommendations_are_ordered_by_score_descending():
    """The page is a ranking now, not a filtered set, so the order is the whole product."""
    import inspect

    assert re.search(r"ORDER BY\s+m\.llm_score DESC", inspect.getsource(recommendations))


def test_the_digest_is_bounded_by_count_and_not_by_score():
    """A quality line that is right in a busy week silently sends nothing in a quiet one."""
    import inspect

    from trouveur.db.queries.match import pending_digest

    body = inspect.getsource(pending_digest)
    assert ":limit" in body and ":threshold" not in body
    assert "llm_score >=" not in body


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


def test_no_query_bundles_two_statements():
    """asyncpg runs parameterised queries as prepared statements, which reject multiple commands.

    Prepending `SET LOCAL hnsw.ef_search = ...;` to the dense SELECT is the natural way to write
    it and fails at runtime, only ever against a real database — so it is asserted here, where
    the suite has none.
    """
    from trouveur.db.queries import jobs, match

    statements = {
        "dense": match._DENSE_SQL,
        "ef_search": match._EF_SEARCH_SQL,
        "lexical": match._LEXICAL_SQL,
        "search": match._SEARCH_SQL,
        "write_embeddings": jobs._WRITE_EMBEDDINGS,
    }
    for name, sql in statements.items():
        assert ";" not in sql.strip().rstrip(";"), f"{name} bundles more than one statement"


def test_parameters_carry_explicit_types_where_context_cannot_infer_them():
    """asyncpg infers a parameter's type from its position and cannot always do so.

    A bare placeholder compared against a literal, or used to build an ARRAY, raises
    "could not determine data type of parameter" at runtime rather than at import.
    """
    from trouveur.db.queries.match import _HARD_FILTERS, _SEARCH_SQL

    assert "CAST(:min_salary AS numeric)" in _HARD_FILTERS
    for cast in (
        "CAST(:query AS text)",
        "CAST(:country AS text)",
        "CAST(:work_mode AS text)",
        "CAST(:include_closed AS boolean)",
    ):
        assert cast in _SEARCH_SQL


def test_array_parameters_are_cast_in_raw_sql():
    """`ANY(:param)` raises "could not determine data type of parameter" on asyncpg.

    Scanned rather than spot-checked, so a new query cannot reintroduce it.
    """
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("trouveur/db/queries").glob("*.py"):
        for match in re.finditer(r"ANY\(:\w+\)", path.read_text()):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert not offenders, f"uncast array parameters: {offenders}"
