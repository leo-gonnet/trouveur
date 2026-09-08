"""Reciprocal rank fusion.

Retrieval runs several independent searches -- one dense search per expanded query, plus a lexical
search -- and they disagree, which is the point. Dense recall finds a Lean Management advert for
someone who wrote "Prozessoptimierung"; lexical recall finds the rare token a vector smears into
its nearest common concept.

Fused by rank rather than by score on purpose. The two systems' scores are not comparable and
never will be: a cosine distance and a ts_rank have different scales, different distributions and
no shared zero, so any weighted sum needs a normalisation constant that has to be retuned every
time either side changes. Ranks are comparable by construction.
"""

from __future__ import annotations

from collections import defaultdict

# The usual constant from the original RRF paper. It damps the top of each list so that one system
# ranking something first cannot by itself dominate the fusion.
RRF_K = 60


def reciprocal_rank_fusion(
    ranked_lists: list[list[int]], *, k: int = RRF_K
) -> list[tuple[int, float]]:
    """Fuse ranked id lists into one, best first."""
    scores: dict[int, float] = defaultdict(float)
    for ranked in ranked_lists:
        for position, job_id in enumerate(ranked, start=1):
            scores[job_id] += 1.0 / (k + position)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def best_ranks(ranked_lists: list[list[int]]) -> dict[int, int]:
    """Each id's best position across the given lists, for reporting which system found what."""
    best: dict[int, int] = {}
    for ranked in ranked_lists:
        for position, job_id in enumerate(ranked, start=1):
            if job_id not in best or position < best[job_id]:
                best[job_id] = position
    return best
