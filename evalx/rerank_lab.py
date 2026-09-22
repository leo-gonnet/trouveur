"""The four things nobody had checked about the paid stage, measured against the reference set.

Every experiment here scores real postings drawn from production's own retrieval, and grades the
resulting order against `reference.json` rather than against the reranker itself. Run
`evalx.reference` first; without it these produce orderings with nothing to compare them to.

The experiments, in the order brief 02 puts them:

  stability      the same postings scored several times, at temperature 0 with the provider
                 pinned. If the spread is large, two runs disagree about what the user reads
                 first, and the page order is not a property of the corpus.
  position       the same batch of ten in rotated orders, to separate a posting's score from
                 where it sat in the prompt.
  contamination  one posting scored alone, then beside nine strong ones, then beside nine weak
                 ones. Batched scoring is comparative whether or not it was asked to be.
  batch          the production shortlist at several batch sizes, against the reference.
  form           absolute 0-100 against a four-band grade. Absolute scoring is what drifts;
                 the question is whether a coarser answer orders as well and repeats better.
  depth          where the judged quality of the retrieval ranking runs out, which is the only
                 evidence there has ever been for rerank_limit = 150.
  background     brief 04's half of this: the same candidates scored with and without the
                 distilled background in the prompt.

Nothing here writes to `user_job_match` or the score cache. An experiment that reused a user's
cache would silently read a stored verdict instead of calling the model, and measure nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
from decimal import Decimal
from pathlib import Path

from evalx import live
from evalx.reference import GOOD, load_store
from trouveur.config import get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import match as match_q
from trouveur.eval.harness import _ensure_persona, load_personas
from trouveur.match import llm, rerank

OUT = Path(__file__).parent / "rerank_results.json"
SHORTLIST = 150  # the production rerank_limit, and the length of the page it pays for
CONCURRENCY = 4

# A four-band answer instead of a number out of a hundred. Kept here rather than in rerank.py
# because it is a candidate, not the prompt anyone runs: if it wins, it moves there. Ties are
# broken by retrieval rank, which is free and already ordered, so the bands do not need to
# invent a within-batch ordering they cannot merge across batches.
_BANDED_SYSTEM = """You screen job adverts for one candidate. You are strict and concise.

Put each job in exactly one band:
  3  excellent fit, the candidate should apply today
  2  good fit, clearly worth reading
  1  plausible but compromised on an important dimension
  0  poor fit

Judge the substance of the role, not the polish of the advert. German and English adverts are
equally valid and neither is preferred. Penalise heavily: staffing agencies, disguised sales
roles, internships and working-student roles, and roles far junior or far senior to the
candidate. Preferred cities are a preference, not a requirement: a role there, nearby, or remote
satisfies it; elsewhere is a compromise, never a rejection.

Return ONLY a JSON array, one object per job, no prose:
[{"id": <int>, "band": <int 0-3>, "reason": "<one sentence, max 25 words>"}]"""


class Spend:
    """One running total for a whole experiment, and one place that refuses to pass the cap."""

    def __init__(self, cap: Decimal) -> None:
        self.cap, self.total = cap, Decimal(0)

    def add(self, cost: Decimal) -> None:
        self.total += cost
        if self.total > self.cap:
            raise llm.BudgetExceeded(f"experiment cap of ${self.cap} passed at ${self.total:.4f}")


def ndcg(order: list[int], grades: dict[int, int], k: int) -> float:
    """Graded gain, so a band-3 posting at rank 1 is worth more than two band-2s.

    Unjudged postings score zero gain rather than being dropped: they were in the pool, so a
    configuration that ranks one highly really did spend a slot on something no judge endorsed.
    """
    def dcg(values: list[int]) -> float:
        return sum((2**value - 1) / math.log2(index + 2) for index, value in enumerate(values[:k]))

    actual = [grades.get(job_id, 0) for job_id in order]
    best = sorted(grades.values(), reverse=True)
    ideal = dcg(best)
    return round(dcg(actual) / ideal, 4) if ideal else 0.0


def spearman(left: dict[int, int], right: dict[int, int], fallback: list[int]) -> float:
    """Rank correlation between the two page orders these runs would produce.

    Ties are broken by retrieval rank, exactly as `order_of` does, because the question is
    whether the reader sees the same page twice -- not whether two score vectors correlate. A
    scorer that returned identical scores both times would be perfectly stable by this measure,
    which is the right answer.
    """
    shared = set(left) & set(right)
    if len(shared) < 3:
        return 0.0
    def ranked(scores):
        order = order_of({job: scores[job] for job in shared}, fallback)
        return {job: index for index, job in enumerate(order)}
    a, b = ranked(left), ranked(right)
    n = len(shared)
    d2 = sum((a[job] - b[job]) ** 2 for job in shared)
    return round(1 - (6 * d2) / (n * (n**2 - 1)), 4)


def jaccard(left: list[int], right: list[int]) -> float:
    a, b = set(left), set(right)
    return round(len(a & b) / len(a | b), 4) if a | b else 0.0


def order_of(scores: dict[int, int], fallback: list[int]) -> list[int]:
    """Scores into a page order, ties broken by retrieval rank as the page itself does."""
    rank = {job_id: index for index, job_id in enumerate(fallback)}
    return sorted(scores, key=lambda job: (-scores[job], rank.get(job, 10**6)))


async def _shortlist(conn, persona_key: str, variant: str, limit: int) -> list:
    """The postings production would have paid to score, in retrieval order."""
    store = load_store()
    ids = store["pools"][persona_key][variant][:limit]
    rows = {row.job_id: row for row in await match_q.scoreable_rows(conn, ids)}
    return [rows[job_id] for job_id in ids if job_id in rows]


async def score_all(
    settings, profile, rows: list, *, key: str, model: str, spend: Spend,
    batch_size: int = rerank.BATCH_SIZE, background: str = "", banded: bool = False,
) -> dict[int, int]:
    """One configuration's verdict on a whole shortlist, as {job_id: score}."""
    batches = [rows[start : start + batch_size] for start in range(0, len(rows), batch_size)]
    gate = asyncio.Semaphore(CONCURRENCY)
    scores: dict[int, int] = {}

    async def one(batch):
        async with gate:
            if banded:
                completion = await llm.complete(
                    api_key=key, model=model, provider_pin=settings.default_llm_provider,
                    system=_BANDED_SYSTEM,
                    user=rerank.build_prompt(profile, batch, background=background),
                    max_tokens=rerank._MAX_TOKENS, timeout=settings.llm_timeout_seconds,
                )
                spend.add(completion.usage.cost_usd)
                start, end = completion.text.find("["), completion.text.rfind("]")
                if start == -1:
                    return
                try:
                    payload = json.loads(completion.text[start : end + 1])
                except json.JSONDecodeError:
                    return
                for entry in payload if isinstance(payload, list) else []:
                    if isinstance(entry, dict) and isinstance(entry.get("band"), int):
                        scores[int(entry["id"])] = max(0, min(3, entry["band"]))
                return

            scored, usage = await rerank.score_batch(
                settings, profile, batch, api_key=key, model=model,
                provider_pin=settings.default_llm_provider, background=background,
            )
            spend.add(usage.cost_usd)
            for score in scored:
                scores[score.id] = score.score

    await asyncio.gather(*(one(batch) for batch in batches))
    return scores


async def experiment_stability(conn, settings, key, model, spend, args) -> dict:
    """The same shortlist, several times, nothing changed."""
    out = {}
    for persona in load_personas():
        profile = await _ensure_persona(persona)
        rows = await _shortlist(conn, persona["key"], "nobg", args.limit)
        runs = [
            await score_all(settings, profile, rows, key=key, model=model, spend=spend)
            for _ in range(args.repeats)
        ]
        shared = sorted(set.intersection(*(set(run) for run in runs)))
        spreads = [statistics.pstdev([run[job] for run in runs]) for job in shared]
        ranges = [max(run[job] for run in runs) - min(run[job] for run in runs) for job in shared]
        orders = [order_of(run, [row.job_id for row in rows]) for run in runs]
        pairs = [(i, j) for i in range(len(runs)) for j in range(i + 1, len(runs))]
        out[persona["key"]] = {
            "scored": len(shared),
            "mean_sd": round(statistics.mean(spreads), 2) if spreads else 0,
            "median_range": round(statistics.median(ranges), 1) if ranges else 0,
            "moved_over_10": sum(1 for value in ranges if value > 10),
            "moved_over_20": sum(1 for value in ranges if value > 20),
            "spearman": round(statistics.mean(
                spearman(runs[i], runs[j], [row.job_id for row in rows]) for i, j in pairs), 4),
            "top20_jaccard": round(statistics.mean(
                jaccard(orders[i][:20], orders[j][:20]) for i, j in pairs), 4),
        }
        print(f"  {persona['key']:<24} {out[persona['key']]}", flush=True)
    return out


async def experiment_position(conn, settings, key, model, spend, args) -> dict:
    """One batch of ten, rotated, so every posting visits every slot."""
    out = {}
    for persona in load_personas():
        profile = await _ensure_persona(persona)
        rows = (await _shortlist(conn, persona["key"], "nobg", args.limit))[: rerank.BATCH_SIZE]
        by_slot: dict[int, list[int]] = {slot: [] for slot in range(len(rows))}
        deltas: list[float] = []
        per_job: dict[int, list[int]] = {row.job_id: [] for row in rows}
        for shift in range(len(rows)):
            rotated = rows[shift:] + rows[:shift]
            scores = await score_all(
                settings, profile, rotated, key=key, model=model, spend=spend,
                batch_size=len(rows),
            )
            for slot, row in enumerate(rotated):
                if row.job_id in scores:
                    by_slot[slot].append(scores[row.job_id])
                    per_job[row.job_id].append(scores[row.job_id])
        for values in per_job.values():
            if len(values) > 1:
                deltas.append(max(values) - min(values))
        out[persona["key"]] = {
            "mean_by_slot": {slot: round(statistics.mean(v), 1) if v else None
                             for slot, v in by_slot.items()},
            "first_minus_last": round(
                statistics.mean(by_slot[0]) - statistics.mean(by_slot[len(rows) - 1]), 1
            ) if by_slot[0] and by_slot[len(rows) - 1] else None,
            "median_swing_per_posting": round(statistics.median(deltas), 1) if deltas else 0,
        }
        print(f"  {persona['key']:<24} {out[persona['key']]}", flush=True)
    return out


async def experiment_contamination(conn, settings, key, model, spend, args) -> dict:
    """A posting alone, then among the best of the shortlist, then among the worst.

    The subjects are the middling postings on purpose: a posting the model is sure about will not
    move, and the ones whose placement is actually in question are where a batch effect would
    change what the reader sees first.
    """
    store = load_store()
    out = {}
    for persona in load_personas():
        profile = await _ensure_persona(persona)
        grades = {int(j): g for j, g in store["grades"][args.judge][persona["key"]].items()}
        rows = await _shortlist(conn, persona["key"], "nobg", args.limit)
        strong = [row for row in rows if grades.get(row.job_id, 0) >= GOOD][: rerank.BATCH_SIZE - 1]
        weak = [row for row in rows if grades.get(row.job_id, 0) == 0][: rerank.BATCH_SIZE - 1]
        if len(strong) < rerank.BATCH_SIZE - 1 or len(weak) < rerank.BATCH_SIZE - 1:
            print(f"  {persona['key']}: not enough graded extremes; skipped")
            continue
        subjects = [row for row in rows if grades.get(row.job_id) == 1][: args.subjects]

        alone, among_strong, among_weak = {}, {}, {}
        for subject in subjects:
            for target, company in ((alone, []), (among_strong, strong), (among_weak, weak)):
                scores = await score_all(
                    settings, profile, [subject, *company], key=key, model=model, spend=spend,
                    batch_size=1 + len(company),
                )
                if subject.job_id in scores:
                    target[subject.job_id] = scores[subject.job_id]

        shared = sorted(set(alone) & set(among_strong) & set(among_weak))
        out[persona["key"]] = {
            "subjects": len(shared),
            "mean_alone": round(statistics.mean(alone[j] for j in shared), 1) if shared else None,
            "mean_among_strong": round(
                statistics.mean(among_strong[j] for j in shared), 1) if shared else None,
            "mean_among_weak": round(
                statistics.mean(among_weak[j] for j in shared), 1) if shared else None,
        }
        print(f"  {persona['key']:<24} {out[persona['key']]}", flush=True)
    return out


async def _graded_run(conn, settings, key, model, spend, args, persona, **kwargs) -> dict:
    store = load_store()
    profile = await _ensure_persona(persona)
    grades = {int(j): g for j, g in store["grades"][args.judge][persona["key"]].items()}
    rows = await _shortlist(conn, persona["key"], kwargs.pop("variant", "nobg"), args.limit)
    scores = await score_all(settings, profile, rows, key=key, model=model, spend=spend, **kwargs)
    order = order_of(scores, [row.job_id for row in rows])
    judged = {job: grades[job] for job in order if job in grades}
    return {
        "scored": len(scores),
        "ndcg@20": ndcg(order, judged, 20),
        "ndcg@50": ndcg(order, judged, 50),
        "good@20": sum(1 for job in order[:20] if grades.get(job, 0) >= GOOD),
        "good@50": sum(1 for job in order[:50] if grades.get(job, 0) >= GOOD),
        "order": order,
    }


async def experiment_batch(conn, settings, key, model, spend, args) -> dict:
    out: dict[str, dict] = {}
    for size in (1, 5, 10, 20):
        out[str(size)] = {}
        for persona in load_personas():
            out[str(size)][persona["key"]] = await _graded_run(
                conn, settings, key, model, spend, args, persona, batch_size=size
            )
            print(f"  batch={size:<3} {persona['key']:<24} "
                  f"{ {k: v for k, v in out[str(size)][persona['key']].items() if k != 'order'} }",
                  flush=True)
    return out


async def experiment_form(conn, settings, key, model, spend, args) -> dict:
    out: dict[str, dict] = {
        "absolute": {}, "banded": {}, "absolute_repeat": {}, "banded_repeat": {},
    }
    for persona in load_personas():
        for name, banded in (("absolute", False), ("banded", True)):
            out[name][persona["key"]] = await _graded_run(
                conn, settings, key, model, spend, args, persona, banded=banded
            )
            out[f"{name}_repeat"][persona["key"]] = await _graded_run(
                conn, settings, key, model, spend, args, persona, banded=banded
            )
            first = out[name][persona["key"]]["order"]
            again = out[f"{name}_repeat"][persona["key"]]["order"]
            out[name][persona["key"]]["top20_jaccard_on_repeat"] = jaccard(first[:20], again[:20])
            print(f"  {name:<9} {persona['key']:<24} "
                  f"nDCG@20={out[name][persona['key']]['ndcg@20']} "
                  f"good@20={out[name][persona['key']]['good@20']} "
                  f"repeat_overlap={out[name][persona['key']]['top20_jaccard_on_repeat']}",
                  flush=True)
    return out


async def experiment_background(conn, settings, key, model, spend, args) -> dict:
    """Brief 04 at the paid stage: the same candidates, one line of prompt different.

    Deliberately the same candidate set for both arms -- the `nobg` pool -- so this measures what
    the background does to the *scoring*, with the retrieval difference held out. What it does to
    retrieval is the planted-needle harness's question, not this one's.
    """
    artifacts = await live.load()
    out: dict[str, dict] = {"without": {}, "with": {}}
    for persona in load_personas():
        summary = artifacts[f"{persona['key']}:bg"].summary
        out["without"][persona["key"]] = await _graded_run(
            conn, settings, key, model, spend, args, persona, background=""
        )
        out["with"][persona["key"]] = await _graded_run(
            conn, settings, key, model, spend, args, persona, background=summary
        )
        for name in ("without", "with"):
            row = out[name][persona["key"]]
            print(f"  {name:<8} {persona['key']:<24} nDCG@20={row['ndcg@20']} "
                  f"good@20={row['good@20']} good@50={row['good@50']}", flush=True)
    return out


def experiment_depth(args) -> dict:
    """Free: where the judged quality of the retrieval ranking runs out.

    No model call at all -- the reference set already says what each posting is worth, and the
    pools already say what order retrieval put them in. rerank_limit decides how far down that
    list the user pays to look, so the question is where the good postings stop arriving.
    """
    store = load_store()
    out: dict[str, dict] = {}
    for persona_key, variants in store["pools"].items():
        grades = {int(j): g for j, g in store["grades"][args.judge][persona_key].items()}
        ranked = variants["nobg"]
        out[persona_key] = {
            str(depth): {
                "judged": sum(1 for job in ranked[:depth] if job in grades),
                "good": sum(1 for job in ranked[:depth] if grades.get(job, 0) >= GOOD),
                "excellent": sum(1 for job in ranked[:depth] if grades.get(job, 0) == 3),
            }
            for depth in (25, 50, 75, 100, 125, 150, 175, 200)
        }
        print(f"  {persona_key:<24} "
              f"{ {d: v['good'] for d, v in out[persona_key].items()} }", flush=True)
    return out


EXPERIMENTS = {
    "stability": experiment_stability,
    "position": experiment_position,
    "contamination": experiment_contamination,
    "batch": experiment_batch,
    "form": experiment_form,
    "background": experiment_background,
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiments", help="comma-separated: " + ",".join([*EXPERIMENTS, "depth"]))
    ap.add_argument("--judge", default="openai/gpt-5.1")
    ap.add_argument("--limit", type=int, default=SHORTLIST)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--subjects", type=int, default=6)
    ap.add_argument("--max-usd", type=Decimal, default=Decimal("0.50"))
    args = ap.parse_args()

    key = os.environ["TROUVEUR_EVAL_LLM_KEY"]
    settings = get_settings()
    model = os.environ.get("TROUVEUR_EVAL_LLM_MODEL") or settings.default_llm_model
    spend = Spend(args.max_usd)
    results = json.loads(OUT.read_text("utf-8")) if OUT.exists() else {}
    results.setdefault("model", model)

    for name in args.experiments.split(","):
        print(f"\n== {name}", flush=True)
        try:
            if name == "depth":
                results[name] = experiment_depth(args)
            else:
                async with connect() as conn:
                    results[name] = await EXPERIMENTS[name](
                        conn, settings, key, model, spend, args
                    )
        except llm.BudgetExceeded as exc:
            print(f"  ! {exc}")
            break
        finally:
            results["spent_usd"] = f"{spend.total:.4f}"
            OUT.write_text(json.dumps(results, indent=1), encoding="utf-8")

    print(f"\ntotal ${spend.total:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
