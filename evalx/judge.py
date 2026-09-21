"""Precision the needles cannot measure, by pooled judging.

Planted needles answer recall and nothing else: an unplanted posting ranking first is unjudged,
not wrong. The question the proposal actually raises is the other one -- whether 65 generated
queries fill the 150 slots that get paid for and displayed with roles the user does not want.

So: pool the top of every strategy under test, judge each distinct posting exactly once with the
production reranker (same prompt, same model, same pin as the user's own runs), and score every
strategy against that one judgement set. This is the standard TREC pooling construction. Its
known bias is that a posting no strategy retrieved is never judged, which is why every strategy
contributes to the pool on equal terms.

The judge is an LLM and so is the thing being judged, which is worth stating plainly: the
reranker scores a posting against the profile and never sees the query that retrieved it, so it
cannot prefer a strategy for having phrased itself the way the judge would. It can still be
wrong about the posting -- see the harness note that rerank scores are not reproducible run to
run -- so the numbers below are read as gaps between strategies, not as absolute quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from decimal import Decimal
from pathlib import Path

from evalx import strategies
from evalx.run import RERANK_WINDOW
from trouveur.config import get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import match as match_q
from trouveur.eval.harness import _ensure_persona, load_personas
from trouveur.match import llm, rerank

CACHE = Path(__file__).parent / "judgements.json"
OUT = Path(__file__).parent / "judged_results.json"
GOOD = 70  # the reranker's own "good fit, clearly worth reading" band


def ndcg(scores: list[int], ideal: list[int], k: int) -> float:
    def dcg(values: list[int]) -> float:
        return sum(v / math.log2(i + 2) for i, v in enumerate(values[:k]))
    best = dcg(sorted(ideal, reverse=True))
    return round(dcg(scores) / best, 4) if best else 0.0


async def judge_pool(conn, settings, profile, job_ids: list[int], key: str, model: str,
                     cache: dict) -> Decimal:
    todo = [j for j in job_ids if str(j) not in cache]
    cost = Decimal(0)
    rows = await match_q.scoreable_rows(conn, todo)
    by_id = {r.job_id: r for r in rows}
    ordered = [by_id[j] for j in todo if j in by_id]
    for start in range(0, len(ordered), rerank.BATCH_SIZE):
        batch = ordered[start : start + rerank.BATCH_SIZE]
        try:
            scored, usage = await rerank.score_batch(
                settings, profile, batch, api_key=key, model=model,
                provider_pin=settings.default_llm_provider,
            )
        except llm.LlmError as exc:
            print(f"    ! batch failed: {exc}")
            continue
        cost += usage.cost_usd
        for s in scored:
            cache[str(s.id)] = s.score
        print(f"    judged {min(start + rerank.BATCH_SIZE, len(ordered))}/{len(ordered)}"
              f"  ${cost:.4f}", flush=True)
    return cost


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", required=True)
    ap.add_argument("--depth", type=int, default=20, help="top-N per strategy entering the pool")
    args = ap.parse_args()

    key = os.environ["TROUVEUR_EVAL_LLM_KEY"]
    settings = get_settings()
    model = os.environ.get("TROUVEUR_EVAL_LLM_MODEL") or settings.default_llm_model
    names = args.strategies.split(",")
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    total_cost = Decimal(0)
    results: dict[str, dict] = {}

    async with connect() as conn:
        for persona in load_personas():
            profile = await _ensure_persona(persona)
            per_strategy: dict[str, list[int]] = {}
            for name in names:
                qs = strategies.build(name, profile, persona["key"])
                arms = await strategies.retrieve_routed(conn, profile, qs)
                per_strategy[name] = strategies.fuse(arms, RERANK_WINDOW)
            pool = sorted({j for ids in per_strategy.values() for j in ids[: args.depth]})
            print(f"{persona['key']}: pool of {len(pool)} postings from {len(names)} strategies")
            persona_cache = cache.setdefault(persona["key"], {})
            total_cost += await judge_pool(
                conn, settings, profile, pool, key, model, persona_cache
            )
            CACHE.write_text(json.dumps(cache, indent=0), encoding="utf-8")

            judged_pool = [persona_cache[str(j)] for j in pool if str(j) in persona_cache]
            for name, ids in per_strategy.items():
                top = [persona_cache[str(j)] for j in ids[: args.depth] if str(j) in persona_cache]
                results.setdefault(name, {})[persona["key"]] = {
                    "judged": len(top),
                    "mean_score": round(sum(top) / len(top), 1) if top else 0,
                    "good_at_depth": sum(1 for s in top if s >= GOOD),
                    "ndcg": ndcg(top, judged_pool, args.depth),
                    "window_size": len(ids),
                }
    OUT.write_text(json.dumps({"depth": args.depth, "model": model, "results": results},
                              indent=1), encoding="utf-8")

    print(f"\n{'strategy':<16}{'judged':>8}{'mean':>8}{'good':>8}{'nDCG':>9}")
    print("-" * 49)
    for name in names:
        group = results[name].values()
        judged = sum(g["judged"] for g in group)
        mean = sum(g["mean_score"] * g["judged"] for g in group) / judged if judged else 0
        print(f"{name:<16}{judged:>8}{mean:>8.1f}"
              f"{sum(g['good_at_depth'] for g in group):>8}"
              f"{sum(g['ndcg'] for g in group) / len(results[name]):>9.4f}")
    print(f"\ntotal ${total_cost:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
