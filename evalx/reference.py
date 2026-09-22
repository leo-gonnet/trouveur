"""A judged reference set for the reranker, paid for once.

Planted needles answer recall and nothing else: an unplanted posting ranking first is unjudged,
not wrong. `judge.py` already builds a pooled judgement set, but it judges with the production
reranker -- which measures that reranker against itself and cannot say whether it is any good,
only whether one retrieval strategy feeds it better postings than another.

This is the upgrade brief 02 asks for, and it differs from `judge.py` in three ways that matter:

1. **A stronger judge, from another model family.** Production pins a cheap DeepSeek model;
   the judge is `openai/gpt-5.1` by default. A second judge can be run over the same pool to
   report how far two strong models agree, which is the only honest bound on how much any of
   this can be trusted.
2. **One posting per call.** Position bias within a batch and contamination between postings
   sharing a prompt are two of the things brief 02 asks us to measure in the reranker. A
   reference set carrying those same artifacts could not measure them. Judging singly costs
   barely more than judging in tens -- the batch only amortises the profile block, which is a
   fraction of a prompt dominated by the posting.
3. **Graded relevance, not a 0-100 score.** Four bands, because the finding that started this
   brief is that absolute scores are not reproducible: the same needle scored 45 on one run and
   above 70 on the next. A band is a judgement a model can repeat.

What it still cannot do, stated plainly: the judge sees a posting and a profile and never the
query that retrieved it, so it cannot prefer a configuration for phrasing itself the way the
judge would -- but it is an LLM grading an LLM, and a posting no configuration retrieved is
never judged at all. The numbers are gaps between configurations, not absolute quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

from evalx import live
from trouveur.config import get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import match as match_q
from trouveur.eval.harness import EVAL_FRESH_SINCE, _ensure_persona, load_personas
from trouveur.match import llm, rerank, retrieve

STORE = Path(__file__).parent / "reference.json"
JUDGE_MODEL = "openai/gpt-5.1"
# Deep enough to say something about rerank_limit, which is 150 and has never been justified.
POOL_DEPTH = 200
# The judge answers one posting at a time, so the wall clock is entirely concurrency.
CONCURRENCY = 8
GOOD = 2  # bands 2 and 3 are the postings a reader is glad to have been shown

_SYSTEM = """You are an expert technical recruiter grading how relevant one job advert is to one
candidate. You are strict, and you grade the same way every time.

Grade on this four-point scale:
  3  Strong. The role is one the candidate is aiming for and their background supports it. They
     should apply.
  2  Relevant. Worth reading, but compromised on one dimension -- seniority, domain, language,
     location, or a requirement they only partly meet.
  1  Marginal. The same broad field, but the wrong role, the wrong level, or the wrong direction
     for where the candidate says they are going. A reader would skim past it.
  0  Irrelevant. A different profession, or a listing with no real role in it -- a staffing
     agency's open pool, an internship or working-student post, or a role far outside the
     candidate's level.

Judge the substance of the role, not the polish of the advert. German and English adverts are
equally valid and neither is preferred. The candidate's objectives say where they want to go and
their background says what they can already do: a role only the background supports is a 1, and
so is a role only the objectives ask for. Preferred cities are a preference, not a requirement.

You are not told what search produced this advert, and must not guess.

Return ONLY a JSON object, no prose: {"grade": <int 0-3>, "why": "<one sentence, max 20 words>"}
"""


def _dossier(profile) -> str:
    """The candidate, in full. Unlike the reranker's block this carries the raw background rather
    than the summary: the judge is the reference, so it gets everything there is to know."""
    return (
        "CANDIDATE\n"
        f"current title: {profile.title or 'unstated'}\n"
        f"years of experience: {profile.years_experience}\n"
        f"languages: {', '.join(profile.languages) or 'unstated'}\n"
        f"preferred cities: {', '.join(profile.cities) or 'none stated'}\n"
        f"objectives: {profile.objectives or 'unstated'}\n"
        f"background: {profile.background or 'unstated'}\n"
        f"must have: {'; '.join(profile.must_have) or 'none stated'}\n"
        f"minimum salary: {int(profile.min_salary_eur_year)} EUR/year\n"
    )


def _posting(row) -> str:
    """The posting, truncated exactly as the reranker truncates it.

    A judge that read more of the advert than the reranker ever sees would penalise the reranker
    for missing what was never in its prompt, and the gap would look like a quality difference.
    """
    locations = ", ".join(item.get("raw", "") for item in (getattr(row, "locations", None) or []))
    return (
        "JOB ADVERT\n"
        f"title: {row.title}\n"
        f"company: {row.company or 'unknown'}\n"
        f"location: {locations or 'unknown'} | work mode: {row.work_mode or 'unknown'}\n"
        f"description: {(row.description or '')[:rerank._DESCRIPTION_CHARS]}"
    )


def parse_grade(text: str) -> int | None:
    """An unparseable answer leaves the posting unjudged, never graded 0.

    The same rule the reranker follows, for the same reason: a wrong verdict that gets stored is
    worse than a missing one, and here it would silently corrupt the yardstick everything else is
    measured against.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        grade = json.loads(text[start : end + 1]).get("grade")
    except json.JSONDecodeError:
        return None
    return grade if isinstance(grade, int) and 0 <= grade <= 3 else None


async def judge_one(settings, profile, row, *, key: str, model: str) -> tuple[int | None, Decimal]:
    completion = await llm.complete(
        api_key=key,
        model=model,
        provider_pin=None,
        system=_SYSTEM,
        user=f"{_dossier(profile)}\n{_posting(row)}",
        max_tokens=200,
        timeout=settings.llm_timeout_seconds,
    )
    return parse_grade(completion.text), completion.usage.cost_usd


async def build_pools(
    conn, artifacts: dict[str, live.Artifacts]
) -> dict[str, dict[str, list[int]]]:
    """Every persona's retrieval, in both variants, through production's own code path.

    Both variants contribute to the pool on equal terms. That is the TREC construction and it is
    also what keeps brief 04 answerable: if only the background variant were pooled, the
    postings it alone surfaced would be the only ones judged and it would win by construction.
    """
    pools: dict[str, dict[str, list[int]]] = {}
    for persona in load_personas():
        profile = await _ensure_persona(persona)
        pools[persona["key"]] = {}
        for variant in live.VARIANTS:
            found = artifacts[f"{persona['key']}:{variant}"]
            arms = await retrieve.retrieve_arms(
                conn, live.variant_profile(profile, variant),
                found.queries, found.adverts, fresh_since=EVAL_FRESH_SINCE,
            )
            ranked = [job_id for job_id, _ in retrieve.fuse(arms, retrieve.RETRIEVAL_LIMIT)]
            pools[persona["key"]][variant] = ranked[:POOL_DEPTH]
            print(f"  {persona['key']}:{variant:<5} retrieved {len(ranked)}", flush=True)
    return pools


def load_store() -> dict:
    if STORE.exists():
        return json.loads(STORE.read_text("utf-8"))
    return {"pool_depth": POOL_DEPTH, "pools": {}, "grades": {}}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", default=JUDGE_MODEL)
    ap.add_argument("--max-usd", type=Decimal, default=Decimal("1.20"),
                    help="stop before this much has been spent; the eval key is capped")
    ap.add_argument("--repool", action="store_true", help="re-run retrieval even if pools exist")
    args = ap.parse_args()

    key = os.environ["TROUVEUR_EVAL_LLM_KEY"]
    settings = get_settings()
    store = load_store()
    artifacts = await live.load()

    async with connect() as conn:
        if args.repool or not store["pools"]:
            store["pools"] = await build_pools(conn, artifacts)
            STORE.write_text(json.dumps(store, indent=1), encoding="utf-8")

        graded = store["grades"].setdefault(args.judge, {})
        spent = Decimal(0)
        stopped = False

        for persona_key, variants in store["pools"].items():
            profile = await _ensure_persona(
                next(p for p in load_personas() if p["key"] == persona_key)
            )
            mine = graded.setdefault(persona_key, {})
            todo = sorted({j for ids in variants.values() for j in ids} - {int(j) for j in mine})
            rows = await match_q.scoreable_rows(conn, todo)
            print(f"{persona_key}: {len(todo)} of {len(rows) + len(mine)} left to judge",
                  flush=True)

            gate = asyncio.Semaphore(CONCURRENCY)
            lock = asyncio.Lock()

            async def one(row, *, profile=profile, mine=mine, gate=gate, lock=lock):
                nonlocal spent, stopped
                async with gate:
                    if stopped:
                        return
                    try:
                        grade, cost = await judge_one(
                            settings, profile, row, key=key, model=args.judge
                        )
                    except llm.LlmError as exc:
                        print(f"    ! {row.job_id}: {exc}")
                        return
                    async with lock:
                        spent += cost
                        if grade is not None:
                            mine[str(row.job_id)] = grade
                        if spent > args.max_usd:
                            stopped = True
                            print(f"    ! stopping: ${spent:.4f} spent, cap ${args.max_usd}")

            for start in range(0, len(rows), 50):
                await asyncio.gather(*(one(row) for row in rows[start : start + 50]))
                STORE.write_text(json.dumps(store, indent=1), encoding="utf-8")
                print(f"    judged {min(start + 50, len(rows))}/{len(rows)}  ${spent:.4f}",
                      flush=True)
                if stopped:
                    break
            if stopped:
                break

    STORE.write_text(json.dumps(store, indent=1), encoding="utf-8")
    print(f"\njudge {args.judge}  total ${spent:.4f}")
    for persona_key, mine in store["grades"][args.judge].items():
        bands = [0, 0, 0, 0]
        for grade in mine.values():
            bands[grade] += 1
        print(f"  {persona_key:<24} judged {len(mine):>4}  "
              f"0:{bands[0]} 1:{bands[1]} 2:{bands[2]} 3:{bands[3]}")


if __name__ == "__main__":
    asyncio.run(main())
