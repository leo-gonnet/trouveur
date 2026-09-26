"""Retrieval evaluation by planted known items: an unlabelled haystack of real postings, into
which needles are planted whose relevance is known by construction.

Recall is rigorous -- a needle either came back or it did not. Precision is NOT measurable, since
an unplanted posting ranking highly is unjudged rather than wrong; planted negatives are the
usable substitute. Needles are tiered by which retriever should find them (T1 lexical, T2 dense,
T3 dense plus expansion, N never), because one aggregate number cannot say whether the hybrid
earns its cost.

An evaluation, not a test: it produces numbers to compare against a baseline and gates nothing.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trouveur.config import get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import ingest as ingest_q
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.ingest.persist import persist
from trouveur.ingest.workers import drain_dedup, drain_derive, drain_detail, drain_embed
from trouveur.match import retrieve
from trouveur.match.expand import deterministic_queries
from trouveur.models import DocumentKind, RawDocument, UserProfile
from trouveur.work import WorkKind, backlog

log = logging.getLogger(__name__)

DATA = Path(__file__).parent / "data"
BASELINE = Path(__file__).parent / "baseline.json"
POSITIVE_TIERS = ("T1", "T2", "T3")
# Below this, movement is the noise of a haystack re-swept between runs rather than a change.
RANK_SLIDE = 20
# How far the corpus may grow or shrink before a baseline delta stops meaning anything.
CORPUS_TOLERANCE = 1.5
# Read from the environment and never stored: users' keys live in the database and are entered
# through the web UI, and this harness must not become a second way in.
EVAL_LLM_KEY_VAR = "TROUVEUR_EVAL_LLM_KEY"


def load_personas() -> list[dict]:
    return json.loads((DATA / "personas.json").read_text(encoding="utf-8"))


def load_needles() -> list[dict]:
    return json.loads((DATA / "needles.json").read_text(encoding="utf-8"))


@dataclass
class PersonaResult:
    persona: str
    queries: int = 0
    corpus_open: int = 0
    # tier -> {"found": int, "total": int}, per retrieval arm.
    recall: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    negatives_total: int = 0
    negatives_retrieved: int = 0
    inversions: int = 0
    missed: list[str] = field(default_factory=list)
    # Recorded because found/total saturates: at 29 of 30 found, recall can only report a
    # regression, while a needle sliding from rank 12 to 90 is invisible to it.
    ranks: dict[str, int | None] = field(default_factory=dict)
    rerank: dict | None = None


@dataclass
class Scorecard:
    limit: int
    embedding_version: str
    corpus_open: int
    # Not a defect below 100%: the detail queue always drains behind the sweep. Recorded because
    # two runs at different coverage are not comparable.
    corpus_with_description: int = 0
    personas: list[PersonaResult] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "limit": self.limit,
            "embedding_version": self.embedding_version,
            "corpus_open": self.corpus_open,
            "corpus_with_description": self.corpus_with_description,
            "personas": [asdict(p) for p in self.personas],
        }


# Pinned, not read from settings: needles carry fixed dates, so the production horizon would drop
# them and report a recall collapse that says nothing about retrieval.
EVAL_FRESH_SINCE = datetime(2000, 1, 1, tzinfo=UTC)


async def plant_needles(needles: list[dict]) -> dict[str, int]:
    """Archive and persist every needle through the real ingest path.

    Deliberately not inserted directly into `job`: a needle must be normalised and derived by the
    same code as a real posting, or the evaluation measures retrieval over rows the pipeline could
    never actually have produced.
    """
    by_source: dict[str, list[str]] = defaultdict(list)
    # Read off the needles rather than hardcoded: persist only looks a detail up when told the
    # source has that phase, so a hardcoded list silently drops a new source's descriptions.
    detailed_sources = {n["source"] for n in needles if n.get("detail")}
    documents: list[RawDocument] = []
    detail_documents: list[RawDocument] = []

    for needle in needles:
        source = needle["source"]
        external_id = needle.get("external_id") or needle["listing"]["referenznummer"]
        by_source[source].append(external_id)
        documents.append(
            RawDocument(
                source=source, external_id=external_id, kind=DocumentKind.LISTING,
                scope=external_id.split(":")[0] if ":" in external_id else None,
                payload=needle["listing"],
            )
        )
        if needle.get("detail"):
            detail_documents.append(
                RawDocument(
                    source=source, external_id=external_id, kind=DocumentKind.DETAIL,
                    payload=needle["detail"],
                )
            )

    planted: dict[str, int] = {}
    async with connect() as conn:
        await ingest_q.archive_documents(conn, documents + detail_documents)
        for source, external_ids in by_source.items():
            await persist(
                conn, source, external_ids, requires_detail=source in detailed_sources
            )
            # Reuses the ingest lookup, which already returns the mapping needed here.
            stored = await ingest_q.stored_content_hashes(conn, source, external_ids)
            for needle in needles:
                if needle["source"] != source:
                    continue
                external_id = needle.get("external_id") or needle["listing"]["referenznummer"]
                if external_id in stored:
                    planted[needle["id"]] = stored[external_id][0]
                else:
                    log.warning("needle %s did not reach the corpus", needle["id"])
    return planted


async def drain_everything(batch: int = 256) -> None:
    """Derive and embed until the queues empty, so no needle is invisible for want of a vector."""
    while True:
        async with connect() as conn:
            derived = await drain_derive(conn, limit=1000)
        async with connect() as conn:
            embedded = await drain_embed(conn, limit=batch)
        async with connect() as conn:
            marked = await drain_dedup(conn)
        if not derived and not embedded and not marked:
            break
    async with connect() as conn:
        remaining = await backlog(conn)
    stuck = {k: v for k, v in remaining.items() if k != WorkKind.DETAIL.value}
    if stuck:
        log.warning("queues did not drain completely: %s", stuck)


async def _ensure_persona(persona: dict) -> UserProfile:
    """Create or refresh the persona's account, and return the profile retrieval will use."""
    from trouveur.match.pipeline import profile_from_row

    key = persona["key"]
    async with connect() as conn:
        existing = await users_q.get_user_by_username(conn, key)
        user_id = (
            existing.id
            if existing
            else await users_q.create_user(conn, key, "argon2$eval-not-a-login")
        )
        await users_q.save_profile(conn, user_id, dict(persona["profile"]))
        return profile_from_row(await users_q.get_profile(conn, user_id))


async def _rerank_needles(
    conn, settings, profile: UserProfile, mine: list[dict], planted: dict[str, int]
) -> dict:
    """Score this persona's own planted needles with the real reranker, and grade the verdict.

    Only the needles, deliberately. They are the only judged items in the corpus, so scoring the
    whole retrieved shortlist would spend real money to produce numbers nobody can mark: an
    unplanted posting scoring 80 is unjudged, not wrong -- the same reason precision is not
    measurable at retrieval. What this does answer is the question the tiers already set up, one
    stage later:

      - a negative scored at or above a positive is a negative the user reads first, because the
        page is ordered by this score and nothing is filtered out of it;
      - the gap between the worst positive and the best negative is how much room a reader has
        before the two kinds start interleaving.

    Reuses `rerank.score_one`, so the prompt, the model and the provider pin are the ones
    production sends. A copy of the prompt here would grade something the user never runs.
    """
    from trouveur.match import expand, llm, rerank

    api_key = os.environ.get(EVAL_LLM_KEY_VAR)
    if not api_key:
        return {}

    model = os.environ.get("TROUVEUR_EVAL_LLM_MODEL") or settings.default_llm_model
    graded = {
        planted[n["id"]]: n
        for n in mine
        if n["id"] in planted and n["tier"] in (*POSITIVE_TIERS, "N")
    }
    rows = await match_q.scoreable_rows(conn, sorted(graded))
    if not rows:
        return {}

    scores: dict[str, int] = {}
    cost = Decimal(0)
    errors: list[str] = []

    for row in rows:
        try:
            score, usage = await rerank.score_one(
                settings, profile, row,
                api_key=api_key, model=model,
                provider_pin=settings.default_llm_provider,
                # The truncated background, not a distilled one: building one would make this
                # grading depend on a second model's output. The same floor the pipeline uses.
                background=expand.truncated_background(profile),
            )
        except llm.LlmError as exc:
            errors.append(str(exc))
            log.warning("scoring failed for persona %s: %s", profile.user_id, exc)
            break
        cost += usage.cost_usd
        needle = graded.get(row.job_id)
        if score is not None and needle is not None:
            scores[needle["id"]] = score.score

    return {
        "model": model,
        **grade_scores(list(graded.values()), scores),
        "cost_usd": f"{cost:.6f}",
        "errors": errors,
    }


def grade_scores(needles: list[dict], scores: dict[str, int]) -> dict:
    """Grade the reranker by the order it puts the needles in, not against a cut-off.

    There is no threshold to grade against any more: the page shows everything that was scored,
    ordered by the score, so what matters is not whether a posting cleared a line but whether a
    planted negative outranks a planted positive -- that is a bad posting the user reads first.

    `margin` is the distance between the worst positive and the best negative. Positive means the
    two groups are cleanly separated with that much room; negative means they interleave.

    Pure, and separate from the call that produced the scores, so the direction of the comparison
    is testable without spending money.
    """
    positive = {
        n["id"]: scores[n["id"]]
        for n in needles
        if n["tier"] in POSITIVE_TIERS and n["id"] in scores
    }
    negative = {
        n["id"]: scores[n["id"]] for n in needles if n["tier"] == "N" and n["id"] in scores
    }
    return {
        "scored": len(scores),
        "unscored": sum(1 for n in needles if n["id"] not in scores),
        "scores": dict(sorted(scores.items())),
        # Pairs, not postings: one negative above three positives is three things to step over.
        "inversions": sum(
            1 for bad in negative.values() for good in positive.values() if bad >= good
        ),
        # The negatives a reader meets before the worst genuine match.
        "outranking": sorted(
            name
            for name, bad in negative.items()
            if positive and bad >= min(positive.values())
        ),
        "margin": (min(positive.values()) - max(negative.values())) if positive and negative
        else None,
    }


def _score_arm(ranked: list[int], planted: dict[str, int], needles: list[dict]) -> dict:
    """Recall per tier for one ranked list of job ids."""
    found = set(ranked)
    per_tier: dict[str, dict[str, int]] = {}
    for tier in POSITIVE_TIERS:
        ids = [planted[n["id"]] for n in needles if n["tier"] == tier and n["id"] in planted]
        per_tier[tier] = {
            "found": sum(1 for job_id in ids if job_id in found),
            "total": len(ids),
        }
    return per_tier


async def _evaluate_persona(
    persona: dict,
    needles: list[dict],
    planted: dict[str, int],
    limit: int,
    rerank_enabled: bool = False,
) -> PersonaResult:
    mine = [n for n in needles if n["persona"] == persona["key"]]
    profile = await _ensure_persona(persona)
    result = PersonaResult(persona=persona["key"])

    # Deterministic expansion only, so a run costs nothing and does not vary with a model. What
    # expansion adds is therefore not yet a number this produces.
    queries = deterministic_queries(profile)
    result.queries = len(queries)

    async with connect() as conn:
        arms = await retrieve.retrieve_arms(
            conn, profile, queries,
            # The whole haystack, not one sweep's worth: recall over the corpus is the question.
            fresh_since=EVAL_FRESH_SINCE, seen_since=EVAL_FRESH_SINCE,
        )
        fused = [job_id for job_id, _ in retrieve.fuse(arms, limit)]

        lexical_only = [
            job_id for job_id, _ in retrieve.fuse(retrieve.Arms(lexical=arms.lexical), limit)
        ]
        dense_only = [
            job_id for job_id, _ in retrieve.fuse(retrieve.Arms(dense=arms.dense), limit)
        ]

        result.recall = {
            "lexical": _score_arm(lexical_only, planted, mine),
            "dense": _score_arm(dense_only, planted, mine),
            "fused": _score_arm(fused, planted, mine),
        }

        positives = {
            planted[n["id"]]: n["id"]
            for n in mine
            if n["tier"] in POSITIVE_TIERS and n["id"] in planted
        }
        negatives = {
            planted[n["id"]]: n["id"] for n in mine if n["tier"] == "N" and n["id"] in planted
        }
        result.negatives_total = len(negatives)
        result.missed = sorted(
            name for job_id, name in positives.items() if job_id not in set(fused)
        )

        rank_of = {job_id: position for position, job_id in enumerate(fused)}
        result.ranks = {
            name: (rank_of[job_id] + 1 if job_id in rank_of else None)
            for job_id, name in positives.items()
        }
        retrieved_negatives = [job_id for job_id in negatives if job_id in rank_of]
        result.negatives_retrieved = len(retrieved_negatives)
        result.inversions = sum(
            1
            for negative in retrieved_negatives
            for positive in positives
            if positive in rank_of and rank_of[negative] < rank_of[positive]
        )

        if rerank_enabled:
            result.rerank = await _rerank_needles(conn, get_settings(), profile, mine, planted)

    return result


async def run_eval(limit: int = 200, rerank: bool = False) -> Scorecard:
    """Plant the needles into whatever corpus is present, then score every persona."""
    from trouveur.ingest.embed import embedding_version

    needles = load_needles()
    planted = await plant_needles(needles)
    if len(planted) != len(needles):
        log.warning("%d of %d needles reached the corpus", len(planted), len(needles))
    await drain_everything()

    async with connect() as conn:
        overview = await admin_q.corpus_overview(conn)
        coverage = await admin_q.description_coverage(conn)

    card = Scorecard(
        limit=limit,
        embedding_version=embedding_version(),
        corpus_open=int(overview.open),
        corpus_with_description=int(coverage),
    )
    for persona in load_personas():
        result = await _evaluate_persona(persona, needles, planted, limit, rerank)
        result.corpus_open = card.corpus_open
        card.personas.append(result)
    return card


def _bar(found: int, total: int) -> str:
    return f"{found}/{total}" if total else "-"


def render(card: Scorecard, baseline: dict | None) -> str:
    """A scorecard a human reads, with the baseline delta beside every number that has one."""
    mismatch = incomparable(card, baseline)
    previous = (
        {} if mismatch else {p["persona"]: p for p in (baseline or {}).get("personas", [])}
    )
    described = (
        100 * card.corpus_with_description / card.corpus_open if card.corpus_open else 0
    )
    lines = [
        f"corpus: {card.corpus_open:,} open postings, "
        f"{described:.0f}% with descriptions   k={card.limit}",
        f"vectors: {card.embedding_version}",
        "",
        f"{'persona':<24}{'tier':<6}{'lexical':>9}{'dense':>9}{'fused':>9}   {'baseline':>9}",
        "-" * 72,
    ]
    if mismatch:
        lines.insert(3, f"baseline: {mismatch}")
    for persona in card.personas:
        was = previous.get(persona.persona, {}).get("recall", {})
        for tier in POSITIVE_TIERS:
            fused = persona.recall["fused"][tier]
            old = was.get("fused", {}).get(tier)
            delta = ""
            if old:
                if fused["found"] > old["found"]:
                    delta = f"  ↑ was {_bar(old['found'], old['total'])}"
                elif fused["found"] < old["found"]:
                    delta = f"  ↓ was {_bar(old['found'], old['total'])}"
                else:
                    delta = f"  = {_bar(old['found'], old['total'])}"
            lines.append(
                f"{persona.persona if tier == 'T1' else '':<24}{tier:<6}"
                f"{_bar(**persona.recall['lexical'][tier]):>9}"
                f"{_bar(**persona.recall['dense'][tier]):>9}"
                f"{_bar(**fused):>9}   {delta}"
            )
        lines.append(
            f"{'':<24}{'N':<6}"
            f"{persona.negatives_total - persona.negatives_retrieved:>4} filtered"
            f"{persona.inversions:>4} inversions"
        )
        if persona.missed:
            lines.append(f"{'':<24}missed: {', '.join(persona.missed)}")
        if persona.rerank:
            r = persona.rerank
            margin = "-" if r["margin"] is None else f"{r['margin']:+d}"
            lines.append(
                f"{'':<24}rerank: {r['scored']} scored"
                f"{'  ' + str(r['unscored']) + ' unscored' if r['unscored'] else ''}"
                f"   {r['inversions']} inversions   margin {margin}   ${r['cost_usd']}"
            )
            if r["outranking"]:
                lines.append(
                    f"{'':<24}  negatives above the worst positive: "
                    f"{', '.join(r['outranking'])}"
                )
            for error in r["errors"]:
                lines.append(f"{'':<24}  ! {error}")
        lines.append("")

    totals = {arm: {"found": 0, "total": 0} for arm in ("lexical", "dense", "fused")}
    for persona in card.personas:
        for arm in totals:
            for tier in POSITIVE_TIERS:
                totals[arm]["found"] += persona.recall[arm][tier]["found"]
                totals[arm]["total"] += persona.recall[arm][tier]["total"]
    lines.append(
        "overall recall   "
        + "   ".join(f"{arm} {_bar(**totals[arm])}" for arm in ("lexical", "dense", "fused"))
    )

    # The shallower depths are where a change still has somewhere to move, and they are the ones
    # that matter: the reranker's budget is far smaller than k.
    ranks = [rank for persona in card.personas for rank in persona.ranks.values()]
    planted = len(ranks)
    depths = [k for k in (10, 25, 50, 100, card.limit) if k <= card.limit]
    lines.append(
        "recall@k (fused) "
        + "   ".join(
            f"k={k} {sum(1 for r in ranks if r is not None and r <= k)}/{planted}" for k in depths
        )
    )
    reciprocal = sum(1 / rank for rank in ranks if rank is not None)
    lines.append(f"mean reciprocal rank   {reciprocal / planted:.3f}" if planted else "")

    scored = [p.rerank for p in card.personas if p.rerank]
    if scored:
        graded = sum(r["scored"] for r in scored)
        inversions = sum(r["inversions"] for r in scored)
        margins = [r["margin"] for r in scored if r["margin"] is not None]
        cost = sum(Decimal(r["cost_usd"]) for r in scored)
        worst = f"{min(margins):+d}" if margins else "-"
        lines.append(
            f"rerank ({scored[0]['model']})   {graded} needles scored   "
            f"{inversions} negative-over-positive pairs   worst margin {worst}   ${cost:.4f}"
        )
    return "\n".join(lines)


def read_baseline() -> dict | None:
    if not BASELINE.exists():
        return None
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def write_baseline(card: Scorecard) -> None:
    BASELINE.write_text(
        json.dumps(card.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def incomparable(card: Scorecard, baseline: dict | None) -> str | None:
    """Why these two runs may not be differenced, or None if they may.

    Recall is a function of how much the needle had to beat. A run over 75k postings and a
    baseline over 4k are not the same measurement, and subtracting them manufactures regressions
    out of nothing but a bigger haystack. The same argument the Scorecard already makes about
    description coverage applies to size, and more sharply.
    """
    was_open = (baseline or {}).get("corpus_open") or 0
    if not was_open:
        return None
    if 1 / CORPUS_TOLERANCE <= card.corpus_open / was_open <= CORPUS_TOLERANCE:
        return None
    return (
        f"not compared: the baseline was measured over {was_open:,} open postings and this run "
        f"over {card.corpus_open:,}. Recall across corpora of different size is not the same "
        f"measurement; re-baseline before reading a delta."
    )


def regressions(card: Scorecard, baseline: dict | None) -> list[str]:
    """Tiers where fused recall dropped. Reported, never raised -- this is not a test."""
    if not baseline:
        return []
    if reason := incomparable(card, baseline):
        return [reason]
    previous = {p["persona"]: p for p in baseline.get("personas", [])}
    found = []
    for persona in card.personas:
        old = previous.get(persona.persona, {}).get("recall", {}).get("fused", {})
        for tier in POSITIVE_TIERS:
            was = old.get(tier)
            now = persona.recall["fused"][tier]
            if was and now["found"] < was["found"]:
                found.append(
                    f"{persona.persona}/{tier}: {now['found']}/{now['total']} "
                    f"(was {was['found']}/{was['total']})"
                )
        # Inside k but much further down: invisible to recall, and where quality actually moves.
        previous_ranks = previous.get(persona.persona, {}).get("ranks", {})
        for name, rank in persona.ranks.items():
            was_rank = previous_ranks.get(name)
            if was_rank and rank and rank - was_rank > RANK_SLIDE:
                found.append(f"{persona.persona}/{name}: rank {was_rank} -> {rank}")
    return found




# Fields the personas plausibly compete in. A haystack the filters remove for free would flatter
# every number: the needles must compete with plausible neighbours.
HAYSTACK_PARTITIONS = [
    "Maschinenbau",
    "Elektrotechnik",
    "Informatik",
    "Softwareentwicklung",
    "Technische Forschung",
    "Technische Produktionsplanung",
    "Umweltschutz",
    "Mathematik",
]


# A description costs one polite request per posting, so an unbounded drain is hours. What the
# budget does not reach is reported as description coverage rather than hidden.
DETAIL_BUDGET = 6000


async def snapshot_haystack(backfill: bool = False) -> int:
    """Sweep the live corpus to serve as the haystack, then fetch the descriptions.

    Every configured source, because the question this exists to answer is recall over the whole
    corpus: a haystack drawn from one adapter measures that adapter. Arbeitsagentur is the one
    exception -- alone it would contribute tens of thousands of postings a day and drown every
    other source -- so it is narrowed to the occupational fields the personas plausibly compete
    in. That narrowing is also what keeps the haystack hard: filling it with retail vacancies
    would let the hard filters remove most of it for free and flatter every number.
    """
    from trouveur.ingest import pipeline
    from trouveur.sources import build_sources
    from trouveur.sources.arbeitsagentur import ArbeitsagenturSource
    from trouveur.sources.http import PoliteClient

    settings = get_settings()
    async with connect() as conn:
        tenants = await admin_q.enabled_tenants(conn)
    sources = [
        ArbeitsagenturSource(partition_filter=HAYSTACK_PARTITIONS)
        if source.name == ArbeitsagenturSource.name
        else source
        for source in build_sources(tenants=tenants)
    ]

    async with PoliteClient() as client:
        report = await pipeline.sweep_sources(
            client, sources, settings=settings, backfill=backfill
        )
        # One client across the sweep and the drain: a fresh one per round would reset the
        # per-provider budget and turn a polite loop into a burst.
        fetched = await drain_details(client, {source.name: source for source in sources})

    collected = sum(source.documents for source in report.per_source.values())
    log.info("haystack: %d documents, %d descriptions fetched", collected, fetched)
    log.info("haystack per source: %s", report.summary())
    return collected


async def drain_details(client, sources: dict[str, object], budget: int = DETAIL_BUDGET) -> int:
    """Fetch descriptions until the queue empties or the budget runs out.

    A haystack of title-only postings is not the corpus production has: every needle carries a
    hand-written description, so leaving the haystack without one makes the needles distinguishable
    by length alone and the comparison is between unlike things.

    The sharper reason is that `persist` deliberately does not queue a posting for embedding until
    its description has arrived, so an undrained detail queue is not merely a thinner haystack --
    those postings are absent from the ANN index entirely. The dense arm then competes against a
    fraction of what the lexical arm sees, and its recall is flattered by exactly that gap.
    """
    fetched = 0
    while fetched < budget:
        async with connect() as conn:
            done = await drain_detail(conn, client, sources, limit=min(200, budget - fetched))
        if not done:
            return fetched
        fetched += done
        log.info("details: %d fetched", fetched)
    async with connect() as conn:
        left = (await backlog(conn)).get(WorkKind.DETAIL.value, 0)
    if left:
        log.warning(
            "detail budget of %d spent with %d postings still without a description; "
            "the scorecard's description coverage reports what that left",
            budget, left,
        )
    return fetched
