"""Hybrid retrieval, with the arms kept separable.

The pipeline fuses everything; the evaluation harness scores each arm on its own to answer whether
the hybrid is earning its cost. Both call this, so there is one implementation of "what does
retrieval return" and the number the harness reports is the number production produces.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.queries import match as match_q
from trouveur.ingest.embed import get_provider
from trouveur.match.fuse import best_ranks, reciprocal_rank_fusion
from trouveur.models import UserProfile

MIN_PER_QUERY = 25
# Retrieval is free, so it fetches deep for everyone rather than being tuned per user: rerank takes
# the top rerank_limit by retrieval score whatever was fetched, so over-fetching costs nothing. The
# dense arm is bounded below this by
# ef_search in db/queries/match.py, which is fine -- past a couple of hundred neighbours per query
# similarity is noise -- while the lexical arm honours the full budget.
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
) -> Arms:
    """Run every retriever for the queries it can use, without fusing.

    Queries go to both arms. Adverts go to the dense arm ONLY, and this is not a tuning choice:
    `websearch_to_tsquery` ANDs its terms, so a 69-word advert becomes a 65-term conjunction that
    matches nothing. Measured over 224k postings, adverts through the lexical arm returned zero
    rows on both the tsvector and the trigram path, every time -- sending them there spends half
    the query budget on empty results.

    Adverts are ADDED to the queries in the dense arm rather than replacing them. Substituting
    them cost exactly the postings that share the profile's own vocabulary: one planted needle
    fell from rank 31 to 341 when the user's words stopped being searched for directly.

    The per-query budget is the retrieval limit spread across each arm's queries, floored: an arm
    with many queries must not give each a slice so thin that a good match falls off the end.
    """
    if not queries and not adverts:
        return Arms()
    provider = get_provider()
    dense_queries = [*queries, *adverts]
    vectors = await provider.embed_queries(dense_queries) if dense_queries else []
    dense_budget = max(RETRIEVAL_LIMIT // max(len(dense_queries), 1), MIN_PER_QUERY)
    lexical_budget = max(RETRIEVAL_LIMIT // max(len(queries), 1), MIN_PER_QUERY)

    return Arms(
        dense=[
            await match_q.dense_candidates(conn, profile, vector, dense_budget)
            for vector in vectors
        ],
        lexical=[
            await match_q.lexical_candidates(conn, profile, query, lexical_budget)
            for query in queries
        ],
    )


def fuse(arms: Arms, limit: int) -> list[tuple[int, float]]:
    return reciprocal_rank_fusion(arms.all)[:limit]


def ranks(arms: Arms) -> tuple[dict[int, int], dict[int, int]]:
    return best_ranks(arms.dense), best_ranks(arms.lexical)
