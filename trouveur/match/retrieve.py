"""Hybrid retrieval, with the arms kept separable so the evaluation harness can score each one
against the number production actually produces."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.queries import match as match_q
from trouveur.ingest.embed import get_query_provider
from trouveur.match.fuse import best_ranks, reciprocal_rank_fusion
from trouveur.models import UserProfile

MIN_PER_QUERY = 25
# A constant, not a per-user setting: retrieval is free and rerank takes the top rerank_limit
# whatever was fetched, so a per-user value would have no effect to explain.
RETRIEVAL_LIMIT = 2000


@dataclass
class Arms:
    """One ranked list per query per retriever, before fusion."""

    dense: list[list[int]] = field(default_factory=list)
    lexical: list[list[int]] = field(default_factory=list)

    @property
    def all(self) -> list[list[int]]:
        return [*self.dense, *self.lexical]


async def retrieve_arms(
    conn: AsyncConnection,
    profile: UserProfile,
    queries: list[str],
    adverts: Sequence[str] = (),
    *,
    fresh_since: datetime,
) -> Arms:
    """Run every retriever for the queries it can use, without fusing.

    Adverts go to the dense arm ONLY: `websearch_to_tsquery` ANDs its terms, so a 69-word advert
    becomes a conjunction that matched zero rows every time it was measured. They are ADDED to
    the queries rather than replacing them -- substituting cost a needle 310 rank positions.

    `fresh_since` has no default on purpose, so a retrieval number cannot quietly become a
    measurement of the freshness policy instead.
    """
    if not queries and not adverts:
        return Arms()
    provider = get_query_provider()
    dense_queries = [*queries, *adverts]
    vectors = await provider.embed_queries(dense_queries) if dense_queries else []
    dense_budget = max(RETRIEVAL_LIMIT // max(len(dense_queries), 1), MIN_PER_QUERY)
    lexical_budget = max(RETRIEVAL_LIMIT // max(len(queries), 1), MIN_PER_QUERY)

    return Arms(
        dense=[
            await match_q.dense_candidates(conn, profile, vector, dense_budget, fresh_since)
            for vector in vectors
        ],
        lexical=[
            await match_q.lexical_candidates(conn, profile, query, lexical_budget, fresh_since)
            for query in queries
        ],
    )


def fuse(arms: Arms, limit: int) -> list[tuple[int, float]]:
    return reciprocal_rank_fusion(arms.all)[:limit]


def ranks(arms: Arms) -> tuple[dict[int, int], dict[int, int]]:
    return best_ranks(arms.dense), best_ranks(arms.lexical)
