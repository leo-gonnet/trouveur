"""Per-user SQL: retrieval, match state, score cache and the pages that read them."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.schema import llm_score_cache, user_job_match, user_query_expansion

# pgvector applies a WHERE clause AFTER the index walk, so a filtered ANN query can return far
# fewer rows than asked for -- silently, as a short result rather than an error. Over-fetching and
# raising ef_search is the standard remedy: cost a few hundred extra index hops rather than lose
# recall on exactly the users whose filters are narrow.
_DENSE_OVERSAMPLE = 4
_EF_SEARCH = 200

_HARD_FILTERS = """
    j.closed_at IS NULL
    AND (cardinality(CAST(:countries AS text[])) = 0 OR f.countries && CAST(:countries AS text[]))
    AND (cardinality(CAST(:work_modes AS text[])) = 0
         OR f.work_mode::text = ANY(CAST(:work_modes AS text[])))
    AND (cardinality(CAST(:seniorities AS text[])) = 0
         OR f.seniority::text = ANY(CAST(:seniorities AS text[])))
    AND (cardinality(CAST(:employment_types AS text[])) = 0
         OR f.employment_type::text = ANY(CAST(:employment_types AS text[])))
    AND (
        :min_salary <= 0
        OR f.salary_max_eur_year IS NULL
        OR f.salary_max_eur_year >= :min_salary
    )
"""

_DENSE_SQL = f"""
SET LOCAL hnsw.ef_search = {_EF_SEARCH};
SELECT j.id AS job_id
FROM job_embedding e
JOIN job j ON j.id = e.job_id
JOIN job_facet f ON f.job_id = j.id
WHERE {_HARD_FILTERS}
ORDER BY e.embedding <=> CAST(:vector AS halfvec)
LIMIT :limit
"""

# Two lexical paths in one query, because neither is sufficient on German text: the tsvector
# stems and weights but cannot see 'Ingenieur' inside 'Wirtschaftsingenieur', and the unaccented
# trigram column matches inside compounds and folds umlauts but cannot rank.
_LEXICAL_SQL = f"""
SELECT j.id AS job_id
FROM job j
JOIN job_facet f ON f.job_id = j.id
WHERE {_HARD_FILTERS}
  AND (
      j.search_de @@ websearch_to_tsquery('german', :query)
      OR j.search_fold LIKE '%%' || lower(f_unaccent(:query)) || '%%'
  )
ORDER BY
    ts_rank_cd(j.search_de, websearch_to_tsquery('german', :query)) DESC,
    j.posted_at DESC NULLS LAST
LIMIT :limit
"""


def _filter_params(profile: Any) -> dict[str, Any]:
    return {
        "countries": list(profile.countries or []),
        "work_modes": [str(mode) for mode in (profile.work_modes or [])],
        "seniorities": [str(level) for level in (profile.seniorities or [])],
        "employment_types": [str(kind) for kind in (profile.employment_types or [])],
        "min_salary": float(profile.min_salary_eur_year or 0),
    }


async def dense_candidates(
    conn: AsyncConnection, profile: Any, vector: list[float], limit: int
) -> list[int]:
    """Nearest neighbours to one query vector, in rank order."""
    params = {
        **_filter_params(profile),
        "vector": "[" + ",".join(f"{value:.6f}" for value in vector) + "]",
        "limit": limit * _DENSE_OVERSAMPLE,
    }
    rows = await conn.execute(sa.text(_DENSE_SQL), params)
    return [row.job_id for row in rows][:limit]


async def lexical_candidates(
    conn: AsyncConnection, profile: Any, query: str, limit: int
) -> list[int]:
    rows = await conn.execute(
        sa.text(_LEXICAL_SQL), {**_filter_params(profile), "query": query, "limit": limit}
    )
    return [row.job_id for row in rows]


async def upsert_matches(conn: AsyncConnection, rows: Sequence[dict]) -> int:
    """Record what retrieval found, without disturbing what the user has since done about it.

    state, notified_at and the LLM verdict are deliberately not refreshed: they are the user's
    history, not retrieval's output, and re-running retrieval must never un-dismiss a job or make
    an already-sent digest entry look unsent.
    """
    if not rows:
        return 0
    stmt = pg_insert(user_job_match).values(list(rows))
    stmt = stmt.on_conflict_do_update(
        index_elements=[user_job_match.c.user_id, user_job_match.c.job_id],
        set_={
            "profile_version": stmt.excluded.profile_version,
            "retrieval_score": stmt.excluded.retrieval_score,
            "dense_rank": stmt.excluded.dense_rank,
            "lexical_rank": stmt.excluded.lexical_rank,
        },
    )
    return (await conn.execute(stmt)).rowcount or 0


async def apply_rule_verdicts(conn: AsyncConnection, rows: Sequence[dict]) -> None:
    if not rows:
        return
    await conn.execute(
        sa.text(
            """
            UPDATE user_job_match m
            SET rule_verdict = CAST(t.verdict AS rule_verdict), rule_reason = t.reason
            FROM unnest(CAST(:job_ids AS bigint[]), CAST(:verdicts AS text[]),
                        CAST(:reasons AS text[])) AS t(job_id, verdict, reason)
            WHERE m.user_id = :user_id AND m.job_id = t.job_id
            """
        ),
        {
            "user_id": rows[0]["user_id"],
            "job_ids": [row["job_id"] for row in rows],
            "verdicts": [row["rule_verdict"] for row in rows],
            "reasons": [row["rule_reason"] for row in rows],
        },
    )


async def pending_rerank(
    conn: AsyncConnection, user_id: int, profile_version: int, limit: int
) -> list[sa.Row]:
    """Jobs this user has retrieved, passed the rules, and not yet been scored for.

    Ordered by retrieval score so that a budget cut-off keeps the most promising ones rather than
    an arbitrary slice.
    """
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT m.job_id, j.content_hash, j.title, j.company, j.description,
                       j.locations, f.salary_min_eur_year, f.salary_max_eur_year,
                       f.work_mode::text AS work_mode, m.retrieval_score
                FROM user_job_match m
                JOIN job j ON j.id = m.job_id
                JOIN job_facet f ON f.job_id = j.id
                WHERE m.user_id = :user_id
                  AND m.rule_verdict = 'pass'
                  AND j.closed_at IS NULL
                  AND (m.llm_score IS NULL OR m.profile_version < :profile_version)
                ORDER BY m.retrieval_score DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"user_id": user_id, "profile_version": profile_version, "limit": limit},
        )
    )


async def count_pending_rerank(
    conn: AsyncConnection, user_id: int, profile_version: int
) -> int:
    """How many jobs a re-score would cover, so the UI can price an edit before it happens."""
    return int(
        (
            await conn.execute(
                sa.text(
                    """
                    SELECT count(*) FROM user_job_match m
                    JOIN job j ON j.id = m.job_id
                    WHERE m.user_id = :user_id AND m.rule_verdict = 'pass'
                      AND j.closed_at IS NULL
                      AND (m.llm_score IS NULL OR m.profile_version < :profile_version)
                    """
                ),
                {"user_id": user_id, "profile_version": profile_version},
            )
        ).scalar_one()
        or 0
    )


async def pending_rules(
    conn: AsyncConnection, user_id: int, limit: int
) -> list[sa.Row]:
    """Retrieved jobs this user has no rule verdict for yet.

    Distinct from pending_rerank, which selects rows that have ALREADY passed the rules. Reusing
    that query here would mean the rules stage only ever re-judged rows it had previously passed,
    and every newly retrieved job would stay 'unknown' forever -- invisible to recommendations,
    with no error anywhere.
    """
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT m.job_id, j.content_hash, j.title, j.company, j.description
                FROM user_job_match m
                JOIN job j ON j.id = m.job_id
                WHERE m.user_id = :user_id AND m.rule_verdict = 'unknown'
                  AND j.closed_at IS NULL
                ORDER BY m.retrieval_score DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"user_id": user_id, "limit": limit},
        )
    )


async def cached_scores(
    conn: AsyncConnection, user_id: int, profile_version: int, hashes: Sequence[bytes]
) -> dict[bytes, sa.Row]:
    """Scores already paid for. A job is scored once per profile version, ever."""
    if not hashes:
        return {}
    rows = await conn.execute(
        llm_score_cache.select().where(
            llm_score_cache.c.user_id == user_id,
            llm_score_cache.c.profile_version == profile_version,
            llm_score_cache.c.content_hash.in_(list(hashes)),
        )
    )
    return {row.content_hash: row for row in rows}


async def put_cached_scores(conn: AsyncConnection, rows: Sequence[dict]) -> None:
    if not rows:
        return
    stmt = pg_insert(llm_score_cache).values(list(rows))
    await conn.execute(
        stmt.on_conflict_do_nothing(
            index_elements=[
                llm_score_cache.c.content_hash,
                llm_score_cache.c.user_id,
                llm_score_cache.c.profile_version,
            ]
        )
    )


async def apply_scores(conn: AsyncConnection, user_id: int, rows: Sequence[dict]) -> None:
    if not rows:
        return
    await conn.execute(
        sa.text(
            """
            UPDATE user_job_match m
            SET llm_score = t.score, llm_reason = t.reason,
                llm_red_flags = CAST(t.flags AS jsonb),
                profile_version = :profile_version, scored_at = now()
            FROM unnest(CAST(:job_ids AS bigint[]), CAST(:scores AS smallint[]),
                        CAST(:reasons AS text[]), CAST(:flags AS text[]))
                 AS t(job_id, score, reason, flags)
            WHERE m.user_id = :user_id AND m.job_id = t.job_id
            """
        ),
        {
            "user_id": user_id,
            "profile_version": rows[0]["profile_version"],
            "job_ids": [row["job_id"] for row in rows],
            "scores": [row["score"] for row in rows],
            "reasons": [row["reason"] for row in rows],
            "flags": [json.dumps(row["red_flags"]) for row in rows],
        },
    )


async def recommendations(
    conn: AsyncConnection, user_id: int, threshold: int, limit: int = 100
) -> list[sa.Row]:
    """The strict page: retrieved, passed the rules, scored, and above the user's threshold.

    Deliberately narrow and often empty. It is not a filtered view of the corpus; it is the set of
    postings the whole pipeline is prepared to defend. Search is where everything else stays
    visible -- keep the two apart, and keep the distinction here in the query rather than in a
    template, where the next page to be written will quietly get it wrong.
    """
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT j.id, j.public_id, j.url, j.title, j.company, j.posted_at, j.source,
                       j.locations, f.countries, f.cities, f.work_mode::text AS work_mode,
                       f.salary_min_eur_year, f.salary_max_eur_year, f.skills,
                       m.llm_score, m.llm_reason, m.llm_red_flags, m.state::text AS state
                FROM user_job_match m
                JOIN job j ON j.id = m.job_id
                JOIN job_facet f ON f.job_id = j.id
                WHERE m.user_id = :user_id
                  AND m.rule_verdict = 'pass'
                  AND m.llm_score IS NOT NULL
                  AND m.llm_score >= :threshold
                  AND m.state <> 'dismissed'
                  AND j.closed_at IS NULL
                ORDER BY m.llm_score DESC, j.posted_at DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"user_id": user_id, "threshold": threshold, "limit": limit},
        )
    )


# Every scraped posting, whatever the filters decided. A rejected or unscored job stays findable
# here, which is the entire point of the page: adding a verdict or score filter would turn it into
# a second, worse recommendations page and destroy the only view of what was actually collected.
_SEARCH_SQL = """
SELECT j.id, j.public_id, j.url, j.title, j.company, j.posted_at, j.source, j.closed_at,
       j.locations, f.countries, f.cities, f.work_mode::text AS work_mode,
       f.salary_min_eur_year, f.salary_max_eur_year,
       m.llm_score, m.rule_verdict::text AS rule_verdict, m.state::text AS state
FROM job j
LEFT JOIN job_facet f ON f.job_id = j.id
LEFT JOIN user_job_match m ON m.job_id = j.id AND m.user_id = :user_id
WHERE (:query = '' OR j.search_de @@ websearch_to_tsquery('german', :query)
       OR j.search_fold LIKE '%%' || lower(f_unaccent(:query)) || '%%')
  AND (:country = '' OR f.countries @> ARRAY[:country])
  AND (:work_mode = '' OR f.work_mode::text = :work_mode)
  AND (:include_closed OR j.closed_at IS NULL)
ORDER BY
    CASE WHEN :query = '' THEN 0
         ELSE ts_rank_cd(j.search_de, websearch_to_tsquery('german', :query)) END DESC,
    j.posted_at DESC NULLS LAST
LIMIT :limit OFFSET :offset
"""


async def search_jobs(
    conn: AsyncConnection,
    user_id: int,
    *,
    query: str = "",
    country: str = "",
    work_mode: str = "",
    include_closed: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[sa.Row]:
    return list(
        await conn.execute(
            sa.text(_SEARCH_SQL),
            {
                "user_id": user_id,
                "query": query.strip(),
                "country": country,
                "work_mode": work_mode,
                "include_closed": include_closed,
                "limit": limit,
                "offset": offset,
            },
        )
    )


async def get_job(conn: AsyncConnection, user_id: int, public_id: str) -> sa.Row | None:
    return (
        await conn.execute(
            sa.text(
                """
                SELECT j.*, f.countries, f.cities, f.regions, f.work_mode::text AS work_mode,
                       f.seniority::text AS seniority, f.employment_type::text AS employment_type,
                       f.salary_min_eur_year, f.salary_max_eur_year, f.salary_annualised,
                       f.skills, f.is_agency,
                       m.llm_score, m.llm_reason, m.llm_red_flags,
                       m.rule_verdict::text AS rule_verdict, m.rule_reason,
                       m.state::text AS state
                FROM job j
                LEFT JOIN job_facet f ON f.job_id = j.id
                LEFT JOIN user_job_match m ON m.job_id = j.id AND m.user_id = :user_id
                WHERE j.public_id = CAST(:public_id AS uuid)
                """
            ),
            {"user_id": user_id, "public_id": public_id},
        )
    ).one_or_none()


async def set_state(conn: AsyncConnection, user_id: int, job_id: int, state: str) -> None:
    stmt = pg_insert(user_job_match).values(
        user_id=user_id, job_id=job_id, profile_version=0, state=state,
        state_changed_at=sa.func.now(),
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[user_job_match.c.user_id, user_job_match.c.job_id],
            set_={"state": stmt.excluded.state, "state_changed_at": sa.func.now()},
        )
    )


async def pending_digest(
    conn: AsyncConnection, user_id: int, threshold: int, limit: int = 25
) -> list[sa.Row]:
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT j.id, j.url, j.title, j.company, j.locations, j.source,
                       f.salary_min_eur_year, f.salary_max_eur_year,
                       m.llm_score, m.llm_reason
                FROM user_job_match m
                JOIN job j ON j.id = m.job_id
                JOIN job_facet f ON f.job_id = j.id
                WHERE m.user_id = :user_id AND m.notified_at IS NULL
                  AND m.llm_score >= :threshold AND m.state <> 'dismissed'
                  AND j.closed_at IS NULL
                ORDER BY m.llm_score DESC
                LIMIT :limit
                """
            ),
            {"user_id": user_id, "threshold": threshold, "limit": limit},
        )
    )


async def mark_notified(conn: AsyncConnection, user_id: int, job_ids: Sequence[int]) -> None:
    if not job_ids:
        return
    await conn.execute(
        user_job_match.update()
        .where(
            user_job_match.c.user_id == user_id,
            user_job_match.c.job_id.in_(list(job_ids)),
        )
        .values(notified_at=sa.func.now())
    )


async def get_query_expansion(
    conn: AsyncConnection, user_id: int, profile_version: int, expansion_version: int
) -> list[str] | None:
    row = (
        await conn.execute(
            user_query_expansion.select().where(
                user_query_expansion.c.user_id == user_id,
                user_query_expansion.c.profile_version == profile_version,
                user_query_expansion.c.expansion_version == expansion_version,
            )
        )
    ).one_or_none()
    return list(row.queries) if row else None


async def put_query_expansion(
    conn: AsyncConnection,
    user_id: int,
    profile_version: int,
    expansion_version: int,
    queries: Sequence[str],
) -> None:
    stmt = pg_insert(user_query_expansion).values(
        user_id=user_id,
        profile_version=profile_version,
        expansion_version=expansion_version,
        queries=list(queries),
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                user_query_expansion.c.user_id,
                user_query_expansion.c.profile_version,
                user_query_expansion.c.expansion_version,
            ],
            set_={"queries": stmt.excluded.queries},
        )
    )


async def match_stats(conn: AsyncConnection, user_id: int) -> sa.Row:
    return (
        await conn.execute(
            sa.text(
                """
                SELECT count(*) AS retrieved,
                       count(*) FILTER (WHERE rule_verdict = 'pass') AS passed,
                       count(*) FILTER (WHERE llm_score IS NOT NULL) AS scored,
                       count(*) FILTER (WHERE state = 'saved') AS saved,
                       count(*) FILTER (WHERE state = 'applied') AS applied
                FROM user_job_match WHERE user_id = :user_id
                """
            ),
            {"user_id": user_id},
        )
    ).one()
