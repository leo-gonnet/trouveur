"""Guards on query shape.

These cannot execute SQL -- the suite runs with no database, by design -- so they assert the
properties of the statements that a reviewer would otherwise have to re-derive by reading them.
Each one corresponds to a change that would compile, run, and be wrong.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from trouveur.db.queries.match import _LEXICAL_SQL, _SEARCH_SQL, edition, editions

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


def test_an_edition_has_no_score_cut_only_a_page():
    """Still narrower than Search -- scored only -- but nothing inside the day is hidden.

    A threshold hid postings the user had already paid to have scored, behind a number they had to
    guess. The day is the cut. The LIMIT here is a page the reader walks with OFFSET, not a
    ceiling: every posting in the day is reachable, which is the property a threshold broke.
    """
    import inspect

    body = inspect.getsource(edition)
    assert ":threshold" not in body
    assert "llm_score >=" not in body
    assert "LIMIT :limit OFFSET :offset" in body, "a page must be walkable, not a fixed cut"
    assert "ORDER BY e.llm_score DESC" in body


def test_an_edition_is_read_from_its_own_table_not_derived_from_a_score():
    """An edition is a published record, so it is stored rather than recomputed.

    Derived from `user_job_match.scored_at`, a re-score moved a posting out of the day it was
    published in: yesterday's page silently lost a row. Reading the score from that table again
    would bring the same bug back through the columns instead of the WHERE clause.
    """
    import inspect

    for query in (edition, editions):
        body = inspect.getsource(query)
        assert "user_edition_item" in body, f"{query.__name__} does not read the edition table"
        assert "m.scored_at" not in body, f"{query.__name__} still buckets by scored_at"
        assert "m.llm_score" not in body, (
            f"{query.__name__} reads the current score, not the published one"
        )
        assert not re.search(r"WHERE[^;]*j\.posted_at::date", body), (
            f"{query.__name__} buckets by publication date"
        )


def test_a_published_edition_keeps_a_posting_that_has_since_closed():
    """An edition that shrinks as the world moves on is not a record of anything.

    The card already marks a closed posting; dropping the row instead would also make the day's
    count in the dropdown disagree with the rows under it.
    """
    import inspect

    for query in (edition, editions):
        assert "closed_at IS NULL" not in inspect.getsource(query), (
            f"{query.__name__} drops postings that closed after the edition was published"
        )


def test_an_edition_and_its_count_agree_about_dismissed_postings():
    """The reader curating their own page is fine; the two queries disagreeing is not.

    Sharing one clause is the point: written out twice, the list and the dropdown's count drift
    apart and a day reads "23 postings" above 19 rows.
    """
    import inspect

    from trouveur.db.queries.match import _NOT_DISMISSED

    assert "dismissed" in _NOT_DISMISSED
    for query in (edition, editions):
        assert "_NOT_DISMISSED" in inspect.getsource(query), (
            f"{query.__name__} does not share the dismissed filter"
        )


def test_an_edition_is_ordered_by_score_descending():
    """Within a day the page is a ranking, and the order is the whole product."""
    import inspect

    assert re.search(r"ORDER BY\s+e\.llm_score DESC", inspect.getsource(edition))


def test_the_digest_is_bounded_by_count_and_not_by_score():
    """A quality line that is right in a busy week silently sends nothing in a quiet one."""
    import inspect

    from trouveur.db.queries.match import pending_digest

    body = inspect.getsource(pending_digest)
    assert ":limit" in body and ":threshold" not in body
    assert "llm_score >=" not in body


def test_retrieval_filters_exclude_closed_postings():
    from trouveur.db.queries.match import _ELIGIBLE

    assert "j.closed_at IS NULL" in _ELIGIBLE


def test_location_is_the_only_hard_filter():
    """Nothing but location may narrow the query before ranking.

    Each enum filter that used to sit here dropped every posting whose facet was unstated, which
    hid postings the reader had no way to learn existed. Preferences reach the reranker as prompt
    text instead, so reintroducing one of these is a silent recall loss, not a stricter search.
    """
    from trouveur.db.queries.match import _ELIGIBLE

    for facet in ("work_mode", "seniority", "employment_type", "salary"):
        assert f":{facet}" not in _ELIGIBLE
    assert "f.seniority" not in _ELIGIBLE
    assert "f.employment_type" not in _ELIGIBLE
    assert "f.salary" not in _ELIGIBLE


def test_a_posting_that_states_no_country_is_kept():
    """A posting nobody parsed a country out of is not a posting somewhere else."""
    from trouveur.db.queries.match import _ELIGIBLE

    assert "cardinality(f.countries) = 0" in _ELIGIBLE


def test_fully_remote_is_admitted_wherever_it_was_posted():
    """`remote_anywhere` is part of the location filter, not a work-mode filter.

    A role with no office is in no country, so the country it was posted from cannot be used to
    reject it. The only place the real restriction is written is the description, which the
    reranker reads.
    """
    from trouveur.db.queries.match import _ELIGIBLE

    assert "CAST(:remote_anywhere AS boolean) AND f.work_mode = 'remote'" in _ELIGIBLE


def test_an_empty_country_list_matches_everything():
    """cardinality(...) = 0 must short-circuit the filter.

    Without it an empty country list would match nothing rather than anything, and a new user
    would see an empty product with no indication why.
    """
    from trouveur.db.queries.match import _ELIGIBLE

    assert "cardinality(CAST(:countries AS text[])) = 0" in _ELIGIBLE


def test_no_query_bundles_two_statements():
    """asyncpg runs parameterised queries as prepared statements, which reject multiple commands.

    Bundling a `SET LOCAL ...;` ahead of a SELECT to tune one query is the natural way to write it
    and fails at runtime, only ever against a real database — so it is asserted here, where the
    suite has none.
    """
    from trouveur.db.queries import jobs, match
    from trouveur.ingest.embed.base import _COLUMNS, column_for

    statements = {
        "lexical": match._LEXICAL_SQL,
        "search": match._SEARCH_SQL,
    }
    # Both statements are templates now, one per vector space, so every width they can be
    # rendered for is checked rather than the unrendered template that no database ever sees.
    for dim in _COLUMNS:
        column = column_for(dim)
        statements[f"dense[{dim}]"] = match._dense_sql(column)
        statements[f"write_embeddings[{dim}]"] = jobs._WRITE_EMBEDDINGS.format(column=column)
    for name, sql in statements.items():
        assert ";" not in sql.strip().rstrip(";"), f"{name} bundles more than one statement"


def test_parameters_carry_explicit_types_where_context_cannot_infer_them():
    """asyncpg infers a parameter's type from its position and cannot always do so.

    A bare placeholder compared against a literal, or used to build an ARRAY, raises
    "could not determine data type of parameter" at runtime rather than at import.
    """
    from trouveur.db.queries.match import _ELIGIBLE, _SEARCH_SQL

    assert "CAST(:remote_anywhere AS boolean)" in _ELIGIBLE
    assert "CAST(:countries AS text[])" in _ELIGIBLE
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
