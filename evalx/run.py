"""A/B retrieval experiment over the planted needles.

Reuses the repo harness for everything that must match production -- planting a needle through
the real ingest path, building the persona's profile, the retrieval SQL, the fusion -- and varies
only the queries. The needles and personas are the ones already in trouveur/eval/data; the
generated expansions are frozen fixtures written before needles.json was read, so a strategy
cannot be tuned to the answers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from evalx import strategies
from trouveur.db.engine import connect
from trouveur.eval.harness import (
    POSITIVE_TIERS,
    _ensure_persona,
    drain_everything,
    load_needles,
    load_personas,
    plant_needles,
)

DEPTHS = (10, 25, 50, 150, 400, 2000)
RERANK_WINDOW = 150
OUT = Path(__file__).parent / "results.json"


def _tier_recall(found: set[int], planted: dict[str, int], mine: list[dict]) -> dict:
    out = {}
    for tier in POSITIVE_TIERS:
        ids = [planted[n["id"]] for n in mine if n["tier"] == tier and n["id"] in planted]
        out[tier] = {"found": sum(1 for i in ids if i in found), "total": len(ids)}
    return out


async def run_persona(conn, persona: dict, needles, planted, strategy: str) -> dict:
    mine = [n for n in needles if n["persona"] == persona["key"]]
    profile = await _ensure_persona(persona)
    qs = strategies.build(strategy, profile, persona["key"])

    started = time.monotonic()
    arms = await strategies.retrieve_routed(conn, profile, qs)
    elapsed = time.monotonic() - started

    fused = strategies.fuse(arms, strategies.RETRIEVAL_LIMIT)
    dense_only = strategies.fuse(strategies.Arms(dense=arms.dense), strategies.RETRIEVAL_LIMIT)
    lex_only = strategies.fuse(strategies.Arms(lexical=arms.lexical), strategies.RETRIEVAL_LIMIT)

    positives = {
        planted[n["id"]]: n["id"]
        for n in mine if n["tier"] in POSITIVE_TIERS and n["id"] in planted
    }
    negatives = {
        planted[n["id"]]: n["id"] for n in mine if n["tier"] == "N" and n["id"] in planted
    }
    rank_of = {job_id: i + 1 for i, job_id in enumerate(fused)}

    ranks = {name: rank_of.get(job_id) for job_id, name in positives.items()}
    neg_ranks = {name: rank_of.get(job_id) for job_id, name in negatives.items()}
    return {
        "persona": persona["key"],
        "strategy": strategy,
        "queries_dense": 1 if qs.dense_pool and qs.dense else len(qs.dense),
        "queries_lexical": len(qs.lexical),
        "seconds": round(elapsed, 2),
        "retrieved": len(fused),
        "recall": {
            "fused": _tier_recall(set(fused), planted, mine),
            "dense": _tier_recall(set(dense_only), planted, mine),
            "lexical": _tier_recall(set(lex_only), planted, mine),
        },
        "ranks": ranks,
        "negative_ranks": neg_ranks,
        # Negatives the reader meets before a genuine match, inside the window that is actually
        # paid for and shown.
        "inversions_at_window": sum(
            1
            for nr in neg_ranks.values()
            for pr in ranks.values()
            if nr and nr <= RERANK_WINDOW and (pr is None or nr < pr)
        ),
        "negatives_in_window": sum(
            1 for nr in neg_ranks.values() if nr and nr <= RERANK_WINDOW
        ),
    }


def summarise(rows: list[dict]) -> dict:
    by_strategy: dict[str, list[dict]] = {}
    for row in rows:
        by_strategy.setdefault(row["strategy"], []).append(row)
    out = {}
    for strategy, group in by_strategy.items():
        ranks = [r for row in group for r in row["ranks"].values()]
        total = len(ranks)
        found = [r for r in ranks if r is not None]
        tiers = {}
        for tier in POSITIVE_TIERS:
            f = sum(row["recall"]["fused"][tier]["found"] for row in group)
            t = sum(row["recall"]["fused"][tier]["total"] for row in group)
            tiers[tier] = f"{f}/{t}"
        out[strategy] = {
            "recall_at": {k: sum(1 for r in found if r <= k) for k in DEPTHS},
            "positives": total,
            "tiers_fused": tiers,
            "dense_only": "%d/%d" % (
                sum(row["recall"]["dense"][t]["found"] for row in group for t in POSITIVE_TIERS),
                sum(row["recall"]["dense"][t]["total"] for row in group for t in POSITIVE_TIERS),
            ),
            "lexical_only": "%d/%d" % (
                sum(row["recall"]["lexical"][t]["found"] for row in group for t in POSITIVE_TIERS),
                sum(row["recall"]["lexical"][t]["total"] for row in group for t in POSITIVE_TIERS),
            ),
            "mrr": round(sum(1 / r for r in found) / total, 4) if total else 0,
            "median_rank": sorted(found)[len(found) // 2] if found else None,
            "negatives_in_window": sum(row["negatives_in_window"] for row in group),
            "inversions_at_window": sum(row["inversions_at_window"] for row in group),
            "queries": sum(row["queries_dense"] + row["queries_lexical"] for row in group),
            "seconds": round(sum(row["seconds"] for row in group), 1),
        }
    return out


def render(summary: dict) -> str:
    head = (
        f"{'strategy':<16}{'q':>4}{'@10':>6}{'@25':>6}{'@50':>6}{'@150':>7}{'@400':>7}"
        f"{'@2000':>7}{'MRR':>8}{'med':>6}{'T1':>7}{'T2':>6}{'T3':>6}{'negW':>6}{'inv':>5}{'s':>7}"
    )
    lines = [head, "-" * len(head)]
    for name, s in summary.items():
        n = s["positives"]
        r = s["recall_at"]
        lines.append(
            f"{name:<16}{s['queries']:>4}"
            + "".join(
                f"{str(r[k]) + '/' + str(n):>{w}}"
                for k, w in zip(DEPTHS, (6, 6, 6, 7, 7, 7), strict=True)
            )
            + f"{s['mrr']:>8.4f}{str(s['median_rank']):>6}"
            + f"{s['tiers_fused']['T1']:>7}{s['tiers_fused']['T2']:>6}{s['tiers_fused']['T3']:>6}"
            + f"{s['negatives_in_window']:>6}{s['inversions_at_window']:>5}{s['seconds']:>7.1f}"
        )
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies", default=",".join(strategies.STRATEGIES))
    parser.add_argument("--plant", action="store_true", help="plant needles before running")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    needles = load_needles()
    if args.plant:
        planted = await plant_needles(needles)
        await drain_everything()
        (Path(__file__).parent / "planted.json").write_text(json.dumps(planted), encoding="utf-8")
    else:
        planted = json.loads((Path(__file__).parent / "planted.json").read_text(encoding="utf-8"))
    print(f"planted {len(planted)}/{len(needles)} needles")

    rows = []
    personas = load_personas()
    async with connect() as conn:
        for strategy in args.strategies.split(","):
            for persona in personas:
                row = await run_persona(conn, persona, needles, planted, strategy)
                rows.append(row)
                print(
                    f"  {strategy:<16} {persona['key']:<22} "
                    f"{sum(1 for r in row['ranks'].values() if r):>2}/{len(row['ranks'])} found"
                    f"  {row['seconds']:>6.2f}s"
                )
    summary = summarise(rows)
    Path(args.out).write_text(
        json.dumps({"rows": rows, "summary": summary}, indent=1), encoding="utf-8"
    )
    print()
    print(render(summary))


if __name__ == "__main__":
    asyncio.run(main())
