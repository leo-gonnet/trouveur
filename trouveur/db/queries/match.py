"""Per-user SQL: retrieval, match state, score cache and the pages that read them."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from functools import lru_cache
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.queries import freshness
from trouveur.db.schema import (
    llm_score_cache,
    user_edition_item,
    user_job_match,
    user_query_expansion,
)
from trouveur.models import Expansion

# The ONE filter applied before ranking, and deliberately the only one. Every other preference a
# user states -- cities, salary, languages -- reaches the reranker as text instead, because a rule
# that rejected a posting here hid it from the reader with no way to find out it existed. The four
# enum filters this replaced (work mode, seniority, employment type, salary) each also dropped
# every posting whose facet was unstated, which is why the form had to offer a "not stated" tick
# box to undo the filter the user had just set.
#
# Unstated location passes: a posting nobody parsed a country out of is not a posting somewhere
# else. Fully remote passes wherever it is, because for a role with no office the country named
# says nothing about whether the reader can take it -- whether it is remote *for them* is in the
# description, which the reranker reads.
_ELIGIBLE = f"""
    j.closed_at IS NULL
    AND {freshness.sql("j")}
    AND (
        cardinality(CAST(:countries AS text[])) = 0
        OR f.countries && CAST(:countries AS text[])
        OR cardinality(f.countries) = 0
        OR (CAST(:remote_anywhere AS boolean) AND f.work_mode = 'remote')
    )
"""

# There is deliberately NO ANN index on job_embedding, so this is an exact scan over the vectors
# inside the freshness horizon. An HNSW index applies WHERE *after* the graph walk, which silently
# returns fewer rows than asked for the moment a filter is selective -- looking for jobs in one
# small city, the walk spends its whole working set on postings elsewhere and the filter discards
# them. Measured on the production join shape at 250k vectors: 14ms when the filter qualifies 565
# rows, 80ms unfiltered. Bring an index back only if the horizon holds several million vectors.
#
# The READ width, which is not always the width the embed worker is writing: a model change is
# backfilled over days and the dense arm serves the old space meanwhile.
_DENSE_SQL_TEMPLATE = f"""
SELECT j.id AS job_id
FROM job_embedding e
JOIN job j ON j.id = e.job_id
JOIN job_facet f ON f.job_id = j.id
WHERE {_ELIGIBLE}
  AND e.{{column}} IS NOT NULL
ORDER BY e.{{column}} <=> CAST(:vector AS halfvec)
LIMIT :limit
"""


@lru_cache(maxsize=4)
def _dense_sql(column: str) -> str:
    return _DENSE_SQL_TEMPLATE.format(column=column)

# Two lexical paths, because neither is sufficient on German: the tsvector stems and ranks but
# cannot see 'Ingenieur' inside 'Wirtschaftsingenieur'; the trigram column can but cannot rank.
# Change what is searchable and you must change both.
_LEXICAL_SQL = f"""
SELECT j.id AS job_id
FROM job j
JOIN job_facet f ON f.job_id = j.id
WHERE {_ELIGIBLE}
  AND (
      j.search_de @@ websearch_to_tsquery('german', :query)
      OR j.search_fold LIKE '%' || lower(f_unaccent(:query)) || '%'
  )
ORDER BY
    ts_rank_cd(j.search_de, websearch_to_tsquery('german', :query)) DESC,
    j.posted_at DESC NULLS LAST
LIMIT :limit
"""


def _filter_params(profile: Any, fresh_since: datetime) -> dict[str, Any]:
    return {
        "fresh_since": fresh_since,
        "countries": list(profile.countries or []),
        "remote_anywhere": bool(profile.remote_anywhere),
    }


async def dense_candidates(
    conn: AsyncConnection,
    profile: Any,
    vector: list[float],
    limit: int,
    fresh_since: datetime,
) -> list[int]:
    """Nearest neighbours to one query vector, in rank order."""
    params = {
        **_filter_params(profile, fresh_since),
        "vector": "[" + ",".join(f"{value:.6f}" for value in vector) + "]",
        "limit": limit,
    }
    from trouveur.config import get_settings
    from trouveur.ingest.embed.base import column_for

    rows = await conn.execute(
        sa.text(_dense_sql(column_for(get_settings().embedding_read_dim))), params
    )
    return [row.job_id for row in rows]


async def lexical_candidates(
    conn: AsyncConnection, profile: Any, query: str, limit: int, fresh_since: datetime
) -> list[int]:
    rows = await conn.execute(
        sa.text(_LEXICAL_SQL),
        {**_filter_params(profile, fresh_since), "query": query, "limit": limit},
    )
    return [row.job_id for row in rows]


async def upsert_matches(conn: AsyncConnection, rows: Sequence[dict]) -> int:
    """Record what retrieval found, without disturbing what the user has since done about it.

    state, notified_at and the verdict are the user's history, not retrieval's output.
    """
    if not rows:
        return 0
    stmt = pg_insert(user_job_match).values(list(rows))
    stmt = stmt.on_conflict_do_update(
        index_elements=[user_job_match.c.user_id, user_job_match.c.job_id],
        set_={
            # NOT profile_version. It records the profile a posting was SCORED under, and
            # retrieval runs before scoring on every row it finds again -- re-stamping it here
            # made the rows most in need of a re-score look current, so `profile_version <
            # :current` in pending_rerank matched nothing and a profile change kept its old
            # scores. The repair for that used to be wiping every score the user had.
            "retrieval_score": stmt.excluded.retrieval_score,
            "dense_rank": stmt.excluded.dense_rank,
            "lexical_rank": stmt.excluded.lexical_rank,
        },
    )
    return (await conn.execute(stmt)).rowcount or 0


async def pending_rerank(
    conn: AsyncConnection, user_id: int, profile_version: int, limit: int
) -> list[sa.Row]:
    """Jobs this user has retrieved and not yet been scored for, best first."""
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
                  AND j.closed_at IS NULL
                  AND (m.llm_score IS NULL OR m.profile_version < :profile_version)
                ORDER BY m.retrieval_score DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"user_id": user_id, "profile_version": profile_version, "limit": limit},
        )
    )


async def scoreable_rows(conn: AsyncConnection, job_ids: Sequence[int]) -> list[sa.Row]:
    """The columns the reranker's prompt needs, for an explicit set of postings.

    For the evaluation harness, whose planted needles are never written to `user_job_match`.
    """
    if not job_ids:
        return []
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT j.id AS job_id, j.content_hash, j.title, j.company, j.description,
                       j.locations, f.salary_min_eur_year, f.salary_max_eur_year,
                       f.work_mode::text AS work_mode
                FROM job j
                JOIN job_facet f ON f.job_id = j.id
                WHERE j.id = ANY(CAST(:job_ids AS bigint[]))
                """
            ),
            {"job_ids": list(job_ids)},
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
                    WHERE m.user_id = :user_id
                      AND j.closed_at IS NULL
                      AND (m.llm_score IS NULL OR m.profile_version < :profile_version)
                    """
                ),
                {"user_id": user_id, "profile_version": profile_version},
            )
        ).scalar_one()
        or 0
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


# An edition is READ here and WRITTEN below; it is never derived. Keyed on the day it was
# published rather than on posted_at, because Greenhouse's discovery lag is 146 days at p90 and a
# posting published in April but found in September belongs to September's reading list.
#
# A closed posting stays in its edition, with the card's `closed` tag. Dropping it would shrink a
# published day every time the world moved on, which is the opposite of what an edition is for.
# A posting the user DISMISSED is dropped from both the list and the count, so the two agree:
# the reader curating their own page is not the system rewriting history.
_EDITION_STATE = """
    LEFT JOIN user_job_match m ON m.user_id = e.user_id AND m.job_id = e.job_id
"""
_NOT_DISMISSED = "coalesce(m.state::text, 'new') <> 'dismissed'"


async def editions(conn: AsyncConnection, user_id: int) -> list[sa.Row]:
    """Every day this user has an edition for, newest first.

    The profile version comes back so the page can mark a day that was read under a profile the
    user has since changed; a day can span two versions if the profile changed mid-day.
    """
    return list(
        await conn.execute(
            sa.text(
                f"""
                SELECT e.day,
                       count(*) AS postings,
                       min(e.profile_version) AS profile_version,
                       max(e.profile_version) AS profile_version_max
                FROM user_edition_item e
                {_EDITION_STATE}
                WHERE e.user_id = :user_id AND {_NOT_DISMISSED}
                GROUP BY e.day
                ORDER BY e.day DESC
                """
            ),
            {"user_id": user_id},
        )
    )


async def edition(conn: AsyncConnection, user_id: int, day: date) -> list[sa.Row]:
    """One day's edition, best first. No score cut: the day is the cut.

    The score, reason and red flags are the EDITION's, not user_job_match's -- that row holds the
    current verdict, and reading it here would let a later re-score rewrite what this day said.
    """
    return list(
        await conn.execute(
            sa.text(
                f"""
                SELECT j.id, j.public_id, j.url, j.title, j.company, j.posted_at, j.source,
                       j.closed_at, j.locations, f.countries, f.cities,
                       f.work_mode::text AS work_mode,
                       f.salary_min_eur_year, f.salary_max_eur_year,
                       e.llm_score, e.llm_reason, e.llm_red_flags,
                       coalesce(m.state::text, 'new') AS state
                FROM user_edition_item e
                JOIN job j ON j.id = e.job_id
                JOIN job_facet f ON f.job_id = j.id
                {_EDITION_STATE}
                WHERE e.user_id = :user_id
                  AND e.day = CAST(:day AS date)
                  AND {_NOT_DISMISSED}
                ORDER BY e.llm_score DESC, j.posted_at DESC NULLS LAST
                """
            ),
            {"user_id": user_id, "day": day},
        )
    )


async def edition_size(conn: AsyncConnection, user_id: int, day: date) -> int:
    """How many postings a day's edition holds, so the profile form can say what a save costs."""
    return int(
        (
            await conn.execute(
                sa.text(
                    """
                    SELECT count(*) FROM user_edition_item
                    WHERE user_id = :user_id AND day = CAST(:day AS date)
                    """
                ),
                {"user_id": user_id, "day": day},
            )
        ).scalar_one()
        or 0
    )


async def clear_stale_edition(
    conn: AsyncConnection, user_id: int, day: date, profile_version: int
) -> int:
    """Drop `day`'s edition if it was published under a profile the user has since changed.

    Only ever called for the current day, and only after the user agreed to lose it on the
    profile form: an edition already published is a record, and a past one is never touched.
    Rows at the CURRENT version survive, so a second run on the same day adds to the day
    rather than restarting it.
    """
    return (
        await conn.execute(
            sa.text(
                """
                DELETE FROM user_edition_item
                WHERE user_id = :user_id
                  AND day = CAST(:day AS date)
                  AND profile_version <> :profile_version
                """
            ),
            {"user_id": user_id, "day": day, "profile_version": profile_version},
        )
    ).rowcount or 0


async def publish_edition(conn: AsyncConnection, rows: Sequence[dict]) -> int:
    """Add scored postings to a day's edition.

    Conflicts on (user, day, job) do nothing, so re-running a day is idempotent. A conflict on
    the version constraint is NOT swallowed: that one means a posting is being recommended twice
    under one profile, which is a bug upstream rather than a repeat.
    """
    if not rows:
        return 0
    stmt = pg_insert(user_edition_item).values(list(rows))
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[
            user_edition_item.c.user_id,
            user_edition_item.c.day,
            user_edition_item.c.job_id,
        ]
    )
    return (await conn.execute(stmt)).rowcount or 0


# Every scraped posting, whatever the filters decided. Adding a score filter here would destroy
# the only view of what was actually collected.
_SEARCH_SQL = """
SELECT j.id, j.public_id, j.url, j.title, j.company, j.posted_at, j.source, j.closed_at,
       j.locations, f.countries, f.cities, f.work_mode::text AS work_mode,
       f.salary_min_eur_year, f.salary_max_eur_year,
       m.llm_score, m.llm_reason, m.llm_red_flags, m.state::text AS state
FROM job j
LEFT JOIN job_facet f ON f.job_id = j.id
LEFT JOIN user_job_match m ON m.job_id = j.id AND m.user_id = :user_id
WHERE (CAST(:query AS text) = '' OR j.search_de @@ websearch_to_tsquery('german', :query)
       OR j.search_fold LIKE '%' || lower(f_unaccent(:query)) || '%')
  AND (CAST(:country AS text) = '' OR f.countries @> ARRAY[CAST(:country AS text)])
  AND (CAST(:work_mode AS text) = '' OR f.work_mode::text = CAST(:work_mode AS text))
  AND (CAST(:include_closed AS boolean) OR j.closed_at IS NULL)
ORDER BY
    CASE WHEN CAST(:query AS text) = '' THEN 0
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
    conn: AsyncConnection, user_id: int, limit: int = 25
) -> list[sa.Row]:
    """The best unsent postings for one user, capped by count rather than by score: a threshold
    that is right in a busy week silently sends nothing in a quiet one."""
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
                  AND m.llm_score IS NOT NULL AND m.state <> 'dismissed'
                  AND j.closed_at IS NULL
                ORDER BY m.llm_score DESC
                LIMIT :limit
                """
            ),
            {"user_id": user_id, "limit": limit},
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
) -> Expansion | None:
    """The cached artifacts for this profile version, or None if it has none yet."""
    row = (
        await conn.execute(
            user_query_expansion.select().where(
                user_query_expansion.c.user_id == user_id,
                user_query_expansion.c.profile_version == profile_version,
                user_query_expansion.c.expansion_version == expansion_version,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return Expansion(
        queries=list(row.queries),
        adverts=list(row.adverts or []),
        background_summary=row.background_summary or "",
    )


async def put_query_expansion(
    conn: AsyncConnection,
    user_id: int,
    profile_version: int,
    expansion_version: int,
    expansion: Expansion,
) -> None:
    stmt = pg_insert(user_query_expansion).values(
        user_id=user_id,
        profile_version=profile_version,
        expansion_version=expansion_version,
        queries=list(expansion.queries),
        adverts=list(expansion.adverts),
        background_summary=expansion.background_summary,
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                user_query_expansion.c.user_id,
                user_query_expansion.c.profile_version,
                user_query_expansion.c.expansion_version,
            ],
            set_={
                "queries": stmt.excluded.queries,
                "adverts": stmt.excluded.adverts,
                "background_summary": stmt.excluded.background_summary,
            },
        )
    )


async def match_stats(conn: AsyncConnection, user_id: int) -> sa.Row:
    return (
        await conn.execute(
            sa.text(
                """
                SELECT count(*) AS retrieved,
                       count(*) FILTER (WHERE llm_score IS NOT NULL) AS scored,
                       count(*) FILTER (WHERE state = 'saved') AS saved,
                       count(*) FILTER (WHERE state = 'applied') AS applied
                FROM user_job_match WHERE user_id = :user_id
                """
            ),
            {"user_id": user_id},
        )
    ).one()
