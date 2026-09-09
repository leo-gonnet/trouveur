"""Retrieval evaluation by planted known items.

Labelling a corpus is the expensive part of evaluating search, so this does the opposite: a large
haystack of real postings that nobody labels, into which a small set of hand-written needles is
planted whose relevance is known by construction.

What that buys and what it does not:

  - Recall is measured rigorously. A planted needle either came back or it did not.
  - Precision is NOT measurable. The haystack is real, so an unplanted posting ranking highly is
    unjudged, not wrong. Reporting a precision number here would be inventing one.
  - Planted negatives give a usable substitute: postings the deterministic pipeline must exclude,
    so "did anything that should have been filtered survive" is answerable without judging the
    haystack at all.

Needles are tiered by which retriever should find them, because a single aggregate recall number
cannot answer the question worth asking -- whether the hybrid earns its cost:

  T1  shares vocabulary with the persona          lexical alone should suffice
  T2  same role, no shared vocabulary             only dense should reach it
  T3  adjacent role, different title entirely     needs dense plus query expansion
  N   must be excluded by rules or hard filters   should never survive

This is an evaluation, not a test. It produces numbers to compare against a baseline; it does not
pass or fail, and it never gates a merge.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import ingest as ingest_q
from trouveur.db.queries import jobs as jobs_q
from trouveur.db.queries import users as users_q
from trouveur.ingest.persist import persist
from trouveur.ingest.workers import drain_dedup, drain_derive, drain_embed
from trouveur.match import retrieve
from trouveur.match.expand import deterministic_queries
from trouveur.match.rules import evaluate
from trouveur.models import Candidate, DocumentKind, RawDocument, RuleVerdict, UserProfile
from trouveur.work import WorkKind, backlog

log = logging.getLogger(__name__)

DATA = Path(__file__).parent / "data"
BASELINE = Path(__file__).parent / "baseline.json"
POSITIVE_TIERS = ("T1", "T2", "T3")


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
    negatives_surviving_rules: int = 0
    inversions: int = 0
    missed: list[str] = field(default_factory=list)


@dataclass
class Scorecard:
    limit: int
    embedding_version: str
    corpus_open: int
    # Share of the haystack that has a description. Not a defect to be below 100%: in production
    # the detail queue always drains behind the sweep, so a live corpus is always a mixture. It is
    # recorded because it changes what the numbers mean, and two runs at different coverage are
    # not comparable.
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


async def plant_needles(needles: list[dict]) -> dict[str, int]:
    """Archive and persist every needle through the real ingest path.

    Deliberately not inserted directly into `job`: a needle must be normalised and derived by the
    same code as a real posting, or the evaluation measures retrieval over rows the pipeline could
    never actually have produced.
    """
    by_source: dict[str, list[str]] = defaultdict(list)
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
            await persist(conn, source, external_ids, requires_detail=source == "arbeitsagentur")
            # Reuses the ingest lookup rather than adding a query: it already returns the job id
            # keyed by external id, which is exactly the mapping needed here.
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
    persona: dict, needles: list[dict], planted: dict[str, int], limit: int
) -> PersonaResult:
    mine = [n for n in needles if n["persona"] == persona["key"]]
    profile = await _ensure_persona(persona)
    result = PersonaResult(persona=persona["key"])

    # The deterministic expansion only, so the score does not depend on a model call that costs
    # money and varies between runs. What query expansion adds is a separate measurement.
    queries = deterministic_queries(profile)
    result.queries = len(queries)

    async with connect() as conn:
        arms = await retrieve.retrieve_arms(conn, profile, queries)
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
        retrieved_negatives = [job_id for job_id in negatives if job_id in rank_of]
        result.negatives_retrieved = len(retrieved_negatives)
        result.inversions = sum(
            1
            for negative in retrieved_negatives
            for positive in positives
            if positive in rank_of and rank_of[negative] < rank_of[positive]
        )

        # A negative that survives the rules cut is the one that would reach a paid reranker and
        # then the user, so it is scored after the cut rather than at retrieval. Uses the real
        # rules, not a copy: a second implementation here would grade the wrong thing.
        surviving = 0
        agency = await jobs_q.agency_flags(conn, retrieved_negatives)
        for row in await jobs_q.load_for_derive(conn, retrieved_negatives):
            verdict, _ = evaluate(
                Candidate(
                    job_id=row.id, content_hash=b"", title=row.title,
                    company=row.company, description=row.description,
                    is_agency=agency.get(row.id),
                ),
                profile,
            )
            surviving += int(verdict is RuleVerdict.PASS)
        result.negatives_surviving_rules = surviving

    return result


async def run_eval(limit: int = 200) -> Scorecard:
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
        result = await _evaluate_persona(persona, needles, planted, limit)
        result.corpus_open = card.corpus_open
        card.personas.append(result)
    return card


def _bar(found: int, total: int) -> str:
    return f"{found}/{total}" if total else "-"


def render(card: Scorecard, baseline: dict | None) -> str:
    """A scorecard a human reads, with the baseline delta beside every number that has one."""
    previous = {p["persona"]: p for p in (baseline or {}).get("personas", [])}
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
            f"{persona.negatives_surviving_rules:>5} survived rules"
            f"{persona.inversions:>4} inversions"
        )
        if persona.missed:
            lines.append(f"{'':<24}missed: {', '.join(persona.missed)}")
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
    return "\n".join(lines)


def read_baseline() -> dict | None:
    if not BASELINE.exists():
        return None
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def write_baseline(card: Scorecard) -> None:
    BASELINE.write_text(
        json.dumps(card.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def regressions(card: Scorecard, baseline: dict | None) -> list[str]:
    """Tiers where fused recall dropped. Reported, never raised -- this is not a test."""
    if not baseline:
        return []
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
    return found




# Occupational fields that actually contain work the personas would consider. A haystack of
# retail and logistics postings is a haystack the filters remove for free, which would make every
# recall number flattering and meaningless: the needles must compete with plausible neighbours.
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


async def snapshot_haystack(backfill: bool = False) -> int:
    """Sweep a slice of the live corpus to serve as the haystack.

    Real postings, because the needles must be hard to find. Restricted to occupational fields the
    personas plausibly compete in: filling the haystack with retail vacancies would let the hard
    filters remove most of it for free and flatter every number.
    """
    from trouveur.sources.arbeitsagentur import ArbeitsagenturSource
    from trouveur.sources.http import PoliteClient

    source = ArbeitsagenturSource(partition_filter=HAYSTACK_PARTITIONS)
    collected = 0

    async def sink(documents):
        nonlocal collected
        async with connect() as conn:
            await ingest_q.archive_documents(conn, documents)
            await persist(
                conn, source.name, [d.external_id for d in documents], requires_detail=True
            )
        collected += len(documents)

    async with PoliteClient() as client:
        outcome = await source.sweep(client, sink, backfill=backfill)
    log.info(
        "haystack: %d documents from %d/%d partitions",
        collected, outcome.partitions_done, outcome.partitions_total,
    )
    return collected
