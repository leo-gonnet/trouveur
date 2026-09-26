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

# How many candidates survive fusion and are written to user_job_match. A safety rail rather than
# a volume decision: what a run actually scores is decided by the scorer running out of good
# postings, not by this number.
FUSED_LIMIT = 2000

# How far back a run held after a sweep looks: that sweep's additions, and nothing else. Twenty-
# five rather than twenty-four so a run starting a few minutes late cannot drop a sliver of the
# day -- the hour of overlap is free, because anything already scored is skipped.
#
# A posting therefore gets exactly one chance, on the day it arrives. It used to get seven, which
# meant a quiet week promoted postings that had been beaten by two thousand others for six days
# running: nothing about them had changed except the competition.
NEW_ARRIVALS_HOURS = 25


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
    seen_since: datetime,
) -> Arms:
    """Run every retriever for the queries it can use, without fusing.

    Adverts go to the dense arm ONLY: `websearch_to_tsquery` ANDs its terms, so a 69-word advert
    becomes a conjunction that matched zero rows every time it was measured. They are ADDED to
    the queries rather than replacing them -- substituting cost a needle 310 rank positions.

    Two windows, and they mean different things. `fresh_since` is how old the ADVERT may be, kept
    for the whole retained horizon. `seen_since` is how recently WE got it, and is what makes a
    daily run consider only the last sweep; a run triggered by a profile change passes the whole
    horizon here instead, so the new profile's queries decide what gets re-judged.

    Neither has a default on purpose, so a retrieval number cannot quietly become a measurement of
    the windowing policy instead.
    """
    if not queries and not adverts:
        return Arms()
    provider = get_query_provider()
    dense_queries = [*queries, *adverts]
    vectors = await provider.embed_queries(dense_queries) if dense_queries else []
    return Arms(
        dense=[
            await match_q.dense_candidates(
                conn, profile, vector, PER_QUERY_DEPTH, fresh_since, seen_since
            )
            for vector in vectors
        ],
        lexical=[
            await match_q.lexical_candidates(
                conn, profile, query, PER_QUERY_DEPTH, fresh_since, seen_since
            )
            for query in queries
        ],
    )


def fuse(arms: Arms, limit: int) -> list[tuple[int, float]]:
    return reciprocal_rank_fusion(arms.all)[:limit]


def ranks(arms: Arms) -> tuple[dict[int, int], dict[int, int]]:
    return best_ranks(arms.dense), best_ranks(arms.lexical)
