"""Encoder bake-off: is the embedding model the ceiling?

Everything measured so far varies how the query is phrased. That is, in the end, a way of working
around a weak encoder -- HyDE exists because a symmetric sentence model cannot match a three-word
query to a 400-word advert. This asks the other question: how much of the gap is the model.

Method. A fixed pool -- a seeded random sample of open postings plus every planted needle -- is
embedded by each candidate, and the needles are ranked inside it by exact cosine. Brute force, not
HNSW, so ANN recall is not a confound and the numbers are the model's own. The pool is small
because this box embeds ~180 documents a minute; that is also why the deployment cost of any
change here is a day of backfill, which is part of the answer rather than a footnote to it.

Ranks inside an 8k pool are not ranks inside 224k. They are comparable BETWEEN models, which is
the only comparison this makes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import sqlalchemy as sa

from evalx import strategies
from trouveur.db.engine import connect
from trouveur.eval.harness import POSITIVE_TIERS, _ensure_persona, load_needles, load_personas
from trouveur.ingest.embed.text import embedding_text

HERE = Path(__file__).parent
POOL = HERE / "pool.json"
OUT = HERE / "encoder_results.json"
DEPTHS = (10, 25, 50, 100, 250)

# name -> (query prefix, document prefix). Asymmetric models lose real recall without these, and
# getting it backwards is silent, so they live beside the name exactly as local.py argues.
PREFIXES = {
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
}


# How many vectors one posting gets. The encoder reads 128 tokens and the median posting needs
# 309, so a single vector is a vector of the posting's first third. Chunking is the way to fix
# that WITHOUT changing the model -- at the price of a proportionally larger index and backfill.
CHUNKS = {"chunk2": 2, "chunk3": 3}
_CHUNK_CHARS = 420  # ~128 tokens of German, measured at 2.42 tokens/word


def compose_many(row, variant: str) -> list[str]:
    """Every text this posting contributes. One per vector."""
    n = CHUNKS.get(variant)
    if n is None:
        return [compose(row, variant)]
    description = row.description or ""
    parts = []
    for i in range(n):
        slice_ = description[i * _CHUNK_CHARS : (i + 1) * _CHUNK_CHARS]
        # Every chunk carries the title: a middle slice of prose with no role in it is a vector
        # of some responsibilities belonging to nobody. Short postings repeat their first chunk,
        # which costs an embedding and changes no maximum.
        parts.append("\n".join([row.title, slice_ or description[:_CHUNK_CHARS]]))
    return parts


def compose(row, variant: str) -> str:
    locations = [i.get("raw", "") for i in (row.locations or [])]
    if variant == "current":
        return embedding_text(row.title, row.company, locations, row.description)
    if variant == "title_desc":
        # Company and location are structured filters already; spending scarce encoder tokens on
        # them buys nothing the WHERE clause does not already do.
        return "\n".join([row.title, (row.description or "")[:1200]])
    if variant == "title_desc_long":
        return "\n".join([row.title, (row.description or "")[:6000]])
    raise ValueError(variant)


async def build_pool(size: int) -> dict:
    """A small, hard pool rather than a large, easy one.

    This box embeds ~180 documents a minute, so a pool big enough to be realistic by sheer size
    is not affordable across several models. Difficulty is bought instead of volume: most of the
    pool is the near-misses the current system actually returns for these personas, which is the
    discrimination an encoder exists to make. A haystack of unrelated vacancies mostly measures
    how well a model separates things nobody would confuse.

    Half the hard set comes from the LEXICAL arm, which uses no encoder at all, so those
    distractors cannot have been chosen to flatter or punish any model. The dense half is chosen
    by the incumbent and does bias the pool towards its own confusions -- stated here rather than
    hidden, and the reason the random share exists at all.
    """
    needles = load_needles()
    planted = json.loads((HERE / "planted.json").read_text())
    needle_ids = sorted(planted.values())
    hard: set[int] = set()
    async with connect() as conn:
        for persona in load_personas():
            profile = await _ensure_persona(persona)
            qs = strategies.build("det", profile, persona["key"])
            arms = await strategies.retrieve_routed(conn, profile, qs)
            hard.update(strategies.fuse(strategies.Arms(lexical=arms.lexical), 300))
            hard.update(strategies.fuse(strategies.Arms(dense=arms.dense), 300))
        rows = list(await conn.execute(sa.text(
            "SELECT j.id FROM job j JOIN job_embedding e ON e.job_id = j.id "
            "WHERE j.closed_at IS NULL AND j.description IS NOT NULL AND j.id <> ALL(:ex)"
        ), {"ex": needle_ids}))
    hard -= set(needle_ids)
    random.Random(20260920).shuffle(rows)
    filler = [r.id for r in rows if r.id not in hard][: max(size - len(hard), 0)]
    distractors = sorted(hard) + filler
    pool = {"needle_ids": needle_ids, "distractors": distractors,
            "planted": planted, "hard": len(hard), "size": size,
            "tiers": {n["id"]: n["tier"] for n in needles},
            "persona_of": {n["id"]: n["persona"] for n in needles}}
    POOL.write_text(json.dumps(pool), encoding="utf-8")
    return pool


async def load_texts(ids: list[int], variant: str) -> tuple[list[int], list[str], int]:
    async with connect() as conn:
        rows = list(await conn.execute(sa.text(
            "SELECT id, title, company, locations, description FROM job WHERE id = ANY(:ids)"
        ), {"ids": ids}))
    texts: list[str] = []
    for row in rows:
        texts.extend(compose_many(row, variant))
    per_doc = CHUNKS.get(variant, 1)
    return [r.id for r in rows], texts, per_doc


@lru_cache(maxsize=2)
def _model(model_name: str):
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=model_name)


def embed(model_name: str, texts: list[str], prefix: str, batch: int = 64) -> np.ndarray:
    model = _model(model_name)
    vecs = list(model.embed([f"{prefix}{t}" for t in texts], batch_size=batch))
    arr = np.asarray(vecs, dtype=np.float32)
    # Checked every time, because the failure is silent and expensive: the ONNX build of
    # jina-embeddings-v2-base-de returns all-NaN vectors here, which cosine turns into a ranking
    # indistinguishable from random. Seven hours of embedding produced 0/21 and a plausible-
    # looking "this model is worse" before the vectors themselves were inspected.
    if not np.isfinite(arr).all():
        raise RuntimeError(
            f"{model_name} produced {int(np.isnan(arr).sum())} non-finite values of {arr.size}; "
            "this build of the model is unusable, and any ranking from it would be noise."
        )
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    if (norms == 0).any():
        raise RuntimeError(f"{model_name} produced zero-length vectors; cosine is undefined.")
    return arr / norms


async def evaluate(model_name: str, variant: str, strategy: str, pool: dict) -> dict:
    qp, dp = PREFIXES.get(model_name, ("", ""))
    ids = pool["needle_ids"] + pool["distractors"]
    started = time.time()
    job_ids, texts, per_doc = await load_texts(ids, variant)
    doc = embed(model_name, texts, dp)
    embed_seconds = time.time() - started

    result = {"model": model_name, "variant": variant, "strategy": strategy,
              "pool": len(job_ids), "vectors": len(doc),
              "embed_seconds": round(embed_seconds, 1),
              "dim": int(doc.shape[1]), "ranks": {}}
    for persona in load_personas():
        profile = await _ensure_persona(persona)
        qs = strategies.build(strategy, profile, persona["key"])
        queries = qs.dense
        qv = embed(model_name, queries, qp)
        # One ranked list per query, fused exactly as production fuses: RRF over the dense arm.
        ranked_lists = []
        for row in qv:
            sims = doc @ row
            if per_doc > 1:
                # A posting is as close as its closest chunk. Summing or averaging would let a
                # long advert dilute the one paragraph that actually matches the candidate.
                sims = sims.reshape(len(job_ids), per_doc).max(axis=1)
            order = np.argsort(-sims)
            ranked_lists.append([job_ids[i] for i in order[:2000]])
        fused = [j for j, _ in strategies.reciprocal_rank_fusion(ranked_lists)]
        rank_of = {j: i + 1 for i, j in enumerate(fused)}
        for needle, job_id in pool["planted"].items():
            if pool["persona_of"][needle] != persona["key"]:
                continue
            if pool["tiers"][needle] not in POSITIVE_TIERS:
                continue
            result["ranks"][needle] = rank_of.get(job_id)
    found = [r for r in result["ranks"].values() if r]
    n = len(result["ranks"])
    result["recall_at"] = {k: sum(1 for r in found if r <= k) for k in DEPTHS}
    result["positives"] = n
    result["mrr"] = round(sum(1 / r for r in found) / n, 4) if n else 0
    by_tier = {}
    for tier in POSITIVE_TIERS:
        names = [x for x in result["ranks"] if pool["tiers"][x] == tier]
        by_tier[tier] = sum(1 for x in names if result["ranks"][x] and result["ranks"][x] <= 50)
    result["tier_at50"] = by_tier
    return result


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--variant", default="current")
    ap.add_argument("--strategy", default="det")
    ap.add_argument("--size", type=int, default=8000)
    ap.add_argument("--rebuild-pool", action="store_true")
    args = ap.parse_args()

    pool = (await build_pool(args.size)) if args.rebuild_pool or not POOL.exists() \
        else json.loads(POOL.read_text())
    results = json.loads(OUT.read_text()) if OUT.exists() else []
    for model_name in args.models.split(","):
        r = await evaluate(model_name, args.variant, args.strategy, pool)
        results = [x for x in results if not (
            x["model"] == r["model"] and x["variant"] == r["variant"]
            and x["strategy"] == r["strategy"])]
        results.append(r)
        OUT.write_text(json.dumps(results, indent=1), encoding="utf-8")
        d = r["recall_at"]
        print(f"{model_name:<58} {r['variant']:<15} {args.strategy:<14} dim={r['dim']:<5} "
              f"@10 {d[10]:>2}/{r['positives']}  @50 {d[50]:>2}/{r['positives']}  "
              f"@250 {d[250]:>2}/{r['positives']}  MRR {r['mrr']:.4f}  "
              f"T1/T2/T3@50 {r['tier_at50']['T1']}/{r['tier_at50']['T2']}/{r['tier_at50']['T3']}  "
              f"{r['embed_seconds']:.0f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
