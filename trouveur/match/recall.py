"""The nightly recall check: would search find the jobs each reader kept, if they were new tonight?

The needles are every job the reader saved or applied to, kept for as long as the reader keeps
them. Most are older than the horizon or closed, so tonight's search cannot return them as they
are. The check puts each one back: it takes tonight's candidates per arm, with the distance or
the text rank each sorts by, works out where the needle would have fallen, and fuses the arms as
a run does. The rank it stores is the needle's place in that fused list, or none.

Free: the cached expansion or the deterministic queries, never a new paid one, and vectors from
the same local model the corpus uses.
"""

from __future__ import annotations

import logging
from datetime import datetime

from trouveur.config import Settings
from trouveur.db.engine import connect
from trouveur.db.queries import freshness
from trouveur.db.queries import match as match_q
from trouveur.db.queries import quality as quality_q
from trouveur.db.queries import users as users_q
from trouveur.ingest.embed import get_provider, get_query_provider
from trouveur.ingest.embed.text import embedding_text
from trouveur.match import expand, retrieve
from trouveur.match.pipeline import profile_from_row
from trouveur.versions import QUERY_EXPANSION_VERSION

log = logging.getLogger(__name__)


async def check_all(settings: Settings) -> int:
    """One check per active reader with a profile. Isolated per reader: one bad profile must not
    cost the others their night."""
    async with connect() as conn:
        users = await users_q.list_users(conn)
    checked = 0
    for user in users:
        if not user.is_active:
            continue
        try:
            if await check_user(user.id, settings) is not None:
                checked += 1
        except Exception:
            log.exception("the recall check failed for user %s", user.id)
    return checked


def dense_position(pool_distances: list[float], distance: float) -> int:
    """Where a job at `distance` falls in an arm ordered by distance: after every closer one."""
    return sum(other < distance for other in pool_distances)


def lexical_position(
    pool: list[tuple[float, datetime | None]], rank: float, posted_at: datetime | None
) -> int:
    """Where a job falls in the lexical arm: rank descending, then newest first, undated last."""

    def before(other_rank: float, other_posted: datetime | None) -> bool:
        if other_rank != rank:
            return other_rank > rank
        # SQL orders ties by posted_at alone, so undated peers come in no fixed order: placing the
        # job behind them never claims a find that search may not repeat.
        if other_posted is None:
            return posted_at is None
        return posted_at is None or other_posted > posted_at

    return sum(before(other_rank, other_posted) for other_rank, other_posted in pool)


def insert(pool: list[int], job: int, position: int | None, depth: int) -> list[int]:
    """The arm as it would have been with `job` in it, cut to the arm's depth."""
    others = [other for other in pool if other != job]
    if position is None or position >= depth:
        return others[:depth]
    return (others[:position] + [job] + others[position:])[:depth]


def _arms_with(job: int, dense: list, lexical: list, depth: int) -> retrieve.Arms:
    """Tonight's arms with `job` placed where its distance or text rank puts it in each."""
    arms = retrieve.Arms()
    for ids, distances, needle_distance in dense:
        others = [d for other, d in zip(ids, distances, strict=True) if other != job]
        position = dense_position(others, needle_distance[job]) if job in needle_distance else None
        arms.dense.append(insert(ids, job, position, depth))
    for ids, values, needle_rows in lexical:
        row = needle_rows.get(job)
        position = None
        if row is not None and row.matches:
            others = [v for other, v in zip(ids, values, strict=True) if other != job]
            position = lexical_position(others, float(row.rank), row.posted_at)
        arms.lexical.append(insert(ids, job, position, depth))
    return arms


async def check_user(user_id: int, settings: Settings) -> int | None:
    fresh_since = freshness.fresh_since(settings.retrieval_horizon_days)
    depth = retrieve.PER_QUERY_DEPTH
    async with connect() as conn:
        row = await users_q.get_profile(conn, user_id)
        if row is None:
            return None
        profile = profile_from_row(row)
        # The cached expansion only, never a new one: generating it is a paid call, and this
        # check must stay free. Without a cache it measures the free queries, which is the floor
        # retrieval is meant to stand on anyway.
        expansion = await match_q.get_query_expansion(
            conn, user_id, profile.version, QUERY_EXPANSION_VERSION
        )
        queries = expansion.queries if expansion else expand.deterministic_queries(profile)
        adverts = expansion.adverts if expansion else []
        wanted = await quality_q.needles(conn, user_id)
        ranks: dict[int, int | None] = {job: None for job in wanted}
        if wanted and (queries or adverts):
            reachable = await match_q.in_area(conn, profile, wanted)
            texts = await quality_q.needle_texts(conn, wanted)
            needle_ids = [text.id for text in texts]
            needle_vectors = await get_provider().embed_documents(
                [
                    embedding_text(
                        text.title,
                        text.company,
                        [item["raw"] for item in (text.locations or []) if item.get("raw")],
                        text.description,
                    )
                    for text in texts
                ]
            )
            query_vectors = await get_query_provider().embed_queries([*queries, *adverts])

            dense = []
            for vector in query_vectors:
                pool = await match_q.dense_distances(
                    conn, profile, vector, depth, fresh_since, fresh_since
                )
                needle_distance = dict(
                    zip(
                        needle_ids,
                        await match_q.distances_to(conn, vector, needle_vectors),
                        strict=True,
                    )
                )
                dense.append(
                    (
                        [entry.job_id for entry in pool],
                        [float(entry.distance) for entry in pool],
                        needle_distance,
                    )
                )
            lexical = []
            for query in queries:
                pool = await match_q.lexical_ranks(
                    conn, profile, query, depth, fresh_since, fresh_since
                )
                needle_rows = {
                    entry.job_id: entry for entry in await match_q.lexical_for(conn, query, wanted)
                }
                lexical.append(
                    (
                        [entry.job_id for entry in pool],
                        [(float(entry.rank), entry.posted_at) for entry in pool],
                        needle_rows,
                    )
                )

            for job in wanted:
                if job not in reachable:
                    continue
                fused = [
                    found
                    for found, _ in retrieve.fuse(_arms_with(job, dense, lexical, depth),
                                                  retrieve.FUSED_LIMIT)
                ]
                ranks[job] = fused.index(job) + 1 if job in fused else None
        return await quality_q.record_check(conn, user_id, profile.version, ranks)
