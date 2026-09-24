"""Reciprocal rank fusion.

By RANK, not by score: a cosine distance and a ts_rank have different scales and no shared zero,
so a weighted sum needs a constant retuned every time either side changes.
"""

from __future__ import annotations

from collections import defaultdict

# The constant from the original RRF paper.
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
