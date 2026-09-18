"""Hybrid retrieval, with the arms kept separable.

The pipeline fuses everything; the evaluation harness scores each arm on its own to answer whether
the hybrid is earning its cost. Both call this, so there is one implementation of "what does
retrieval return" and the number the harness reports is the number production produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.queries import match as match_q
from trouveur.ingest.embed import get_provider
from trouveur.match.fuse import best_ranks, reciprocal_rank_fusion
from trouveur.models import UserProfile

MIN_PER_QUERY = 25
RETRIEVAL_HEADROOM = 3
MIN_RETRIEVAL = 100
MAX_RETRIEVAL = 2000


def retrieval_limit_for(rerank_limit: int) -> int:
    """How many candidates to retrieve so that rerank_limit survive the rules cut.

    Retrieving more than will be scored changes nothing past this headroom: the rerank stage takes
    the top rerank_limit by retrieval score, so candidate rerank_limit+1 is never read however many
    were fetched. The multiple is not a user preference, it is an allowance for what the rules
    reject, so it is a constant here rather than a second number to explain on a settings page.
    """
    return min(max(rerank_limit * RETRIEVAL_HEADROOM, MIN_RETRIEVAL), MAX_RETRIEVAL)


@dataclass
class Arms:
    """One ranked list per query per retriever, before fusion."""

    dense: list[list[int]] = field(default_factory=list)
    lexical: list[list[int]] = field(default_factory=list)

    @property
    def all(self) -> list[list[int]]:
        return [*self.dense, *self.lexical]


async def retrieve_arms(
    conn: AsyncConnection, profile: UserProfile, queries: list[str]
) -> Arms:
    """Run every retriever for every expanded query, without fusing.

    The per-query budget is the profile's limit spread across its queries, floored: a profile with
    eight queries must not give each of them a slice so thin that a good match falls off the end.
    """
    if not queries:
        return Arms()
    provider = get_provider()
    vectors = await provider.embed_queries(queries)
    per_query = max(profile.retrieval_limit // len(queries), MIN_PER_QUERY)

    return Arms(
        dense=[
            await match_q.dense_candidates(conn, profile, vector, per_query)
            for vector in vectors
        ],
        lexical=[
            await match_q.lexical_candidates(conn, profile, query, per_query)
            for query in queries
        ],
    )


def fuse(arms: Arms, limit: int) -> list[tuple[int, float]]:
    return reciprocal_rank_fusion(arms.all)[:limit]


def ranks(arms: Arms) -> tuple[dict[int, int], dict[int, int]]:
    return best_ranks(arms.dense), best_ranks(arms.lexical)
