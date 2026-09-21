"""Same queries, four fusion rules, over the full 224k corpus."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evalx import fusion, strategies
from evalx.run import DEPTHS, RERANK_WINDOW
from trouveur.db.engine import connect
from trouveur.eval.harness import POSITIVE_TIERS, _ensure_persona, load_needles, load_personas
from trouveur.match.expand import deterministic_queries

OUT = Path(__file__).parent / "fusion_results.json"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", required=True)
    args = ap.parse_args()

    needles = load_needles()
    planted = json.loads((Path(__file__).parent / "planted.json").read_text())
    tiers = {n["id"]: n["tier"] for n in needles}
    rows = []
    async with connect() as conn:
        for name in args.strategies.split(","):
            for rule in fusion.RULES:
                agg = {"strategy": name, "rule": rule, "ranks": {}}
                for persona in load_personas():
                    profile = await _ensure_persona(persona)
                    qs = strategies.build(name, profile, persona["key"])
                    own = deterministic_queries(profile)
                    arms = await strategies.retrieve_routed(conn, profile, qs)
                    n_own_d = sum(1 for q in qs.dense if q in own)
                    n_own_l = sum(1 for q in qs.lexical if q in own)
                    fused = fusion.fuse(arms, rule, n_own_d, n_own_l)[:2000]
                    rank_of = {j: i + 1 for i, j in enumerate(fused)}
                    for n in needles:
                        if n["persona"] != persona["key"] or n["tier"] not in POSITIVE_TIERS:
                            continue
                        agg["ranks"][n["id"]] = rank_of.get(planted[n["id"]])
                found = [r for r in agg["ranks"].values() if r]
                total = len(agg["ranks"])
                agg["recall_at"] = {k: sum(1 for r in found if r <= k) for k in DEPTHS}
                agg["mrr"] = round(sum(1 / r for r in found) / total, 4)
                agg["window"] = sum(1 for r in found if r <= RERANK_WINDOW)
                agg["tiers"] = {
                    t: sum(1 for i, r in agg["ranks"].items() if tiers[i] == t and r)
                    for t in POSITIVE_TIERS
                }
                rows.append(agg)
                d = agg["recall_at"]
                print(f"{name:<18}{rule:<11}@25 {d[25]:>2}/{total}  @150 {agg['window']:>2}/{total}"
                      f"  @400 {d[400]:>2}/{total}  @2000 {d[2000]:>2}/{total}"
                      f"  MRR {agg['mrr']:.4f}  T1/T2/T3 "
                      f"{agg['tiers']['T1']}/{agg['tiers']['T2']}/{agg['tiers']['T3']}", flush=True)
    OUT.write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
