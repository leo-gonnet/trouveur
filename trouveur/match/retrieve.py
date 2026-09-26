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

# How deep ONE query goes. Deliberately not derived from the fused limit: dividing a total budget
# by the number of queries meant that improving expansion made every individual query shallower --
# sixteen queries got 125 rows each where eight got 250 -- so a better set of search terms bought
# worse coverage per term. The two numbers answer different questions and are now two constants.
#
# Depth is close to free since the dense arm became an exact scan: the scan reads every vector in
# the horizon whatever the LIMIT, so raising this changes only how many rows are sorted out of it.
PER_QUERY_DEPTH = 200

# How many candidates survive fusion and are written to user_job_match. This is the real bound on
# how much a run can score, now that nothing caps the paid stage downstream of it.
FUSED_LIMIT = 2000


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
    return Arms(
        dense=[
            await match_q.dense_candidates(conn, profile, vector, PER_QUERY_DEPTH, fresh_since)
            for vector in vectors
        ],
        lexical=[
            await match_q.lexical_candidates(conn, profile, query, PER_QUERY_DEPTH, fresh_since)
            for query in queries
        ],
    )


def fuse(arms: Arms, limit: int) -> list[tuple[int, float]]:
    return reciprocal_rank_fusion(arms.all)[:limit]


def ranks(arms: Arms) -> tuple[dict[int, int], dict[int, int]]:
    return best_ranks(arms.dense), best_ranks(arms.lexical)
