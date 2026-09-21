"""Does the fusion rule, not the query set, explain the collapse?

Thirty adjacent titles destroyed the ranking: MRR 0.184 -> 0.044, every vocabulary-sharing needle
pushed down. The diagnosis was structural -- RRF sums 1/(k+rank) over every list, so a posting
sitting mid-list in thirty correlated title searches outscores the exact match that appears first
in one. Dropping titles on that basis abandons a query set for a defect in the fusion.

So: same queries, different fusion rules.

  rrf        production: one list per query per arm, fused together
  arm_max    collapse each arm's lists to one by best rank, then fuse the two arms. A posting
             cannot be rewarded for appearing in many near-identical lists, only for appearing
             high in one.
  weighted   RRF with the user's own queries weighted above generated ones, so expansion
             cannot outvote the profile.
  arm_max_w  both
"""

from __future__ import annotations

from collections import defaultdict

from trouveur.match.fuse import RRF_K

# The user's own words count double. Expansion is meant to add reach, not to outvote the profile
# by sheer number of generated lists.
OWN_WEIGHT = 2.0


def _weights(n_own: int, n_total: int) -> list[float]:
    return [OWN_WEIGHT if i < n_own else 1.0 for i in range(n_total)]


def rrf(lists: list[list[int]], weights: list[float] | None = None, k: int = RRF_K):
    scores: dict[int, float] = defaultdict(float)
    weights = weights or [1.0] * len(lists)
    for ranked, weight in zip(lists, weights, strict=True):
        for position, job_id in enumerate(ranked, start=1):
            scores[job_id] += weight / (k + position)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def _collapse(lists: list[list[int]]) -> list[int]:
    """One ranked list per arm, ordered by each posting's best position in any of them."""
    best: dict[int, int] = {}
    for ranked in lists:
        for position, job_id in enumerate(ranked, start=1):
            if job_id not in best or position < best[job_id]:
                best[job_id] = position
    return [job_id for job_id, _ in sorted(best.items(), key=lambda pair: (pair[1], pair[0]))]


def fuse(arms, rule: str, n_own_dense: int = 0, n_own_lexical: int = 0) -> list[int]:
    match rule:
        case "rrf":
            return [j for j, _ in rrf([*arms.dense, *arms.lexical])]
        case "arm_max":
            collapsed = [x for x in (_collapse(arms.dense), _collapse(arms.lexical)) if x]
            return [j for j, _ in rrf(collapsed)]
        case "weighted":
            lists = [*arms.dense, *arms.lexical]
            weights = _weights(n_own_dense, len(arms.dense)) + _weights(
                n_own_lexical, len(arms.lexical)
            )
            return [j for j, _ in rrf(lists, weights)]
        case "arm_max_w":
            dense_own = _collapse(arms.dense[:n_own_dense])
            dense_gen = _collapse(arms.dense[n_own_dense:])
            lexical = _collapse(arms.lexical)
            lists, weights = [], []
            for ranked, weight in ((dense_own, OWN_WEIGHT), (dense_gen, 1.0),
                                   (lexical, OWN_WEIGHT)):
                if ranked:
                    lists.append(ranked)
                    weights.append(weight)
            return [j for j, _ in rrf(lists, weights)]
        case _:
            raise ValueError(rule)


RULES = ["rrf", "arm_max", "weighted", "arm_max_w"]
