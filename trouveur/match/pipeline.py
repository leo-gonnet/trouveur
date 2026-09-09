"""Per-user matching: retrieve, cut, rerank. Orchestration only.

Runs per user in isolation. One user's expired API key, exhausted budget or malformed profile must
never affect another's results, which is also why spend and credentials are per user rather than
per installation.

The three stages are deliberately separate and get cheaper to re-run in that order: retrieval is
free and re-runnable, the rules cut is free, and only the last stage costs money.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.config import Settings, get_settings
from trouveur.crypto import CredentialError, decrypt
from trouveur.db.engine import connect
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.match import expand, llm, rerank, retrieve
from trouveur.match.rules import evaluate
from trouveur.models import Candidate, RuleVerdict, UserProfile
from trouveur.versions import QUERY_EXPANSION_VERSION

log = logging.getLogger(__name__)


@dataclass
class MatchReport:
    user_id: int
    queries: int = 0
    retrieved: int = 0
    passed: int = 0
    rejected: int = 0
    scored: int = 0
    from_cache: int = 0
    cost_usd: Decimal = Decimal(0)
    stopped_on_budget: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"user={self.user_id} queries={self.queries} retrieved={self.retrieved} "
            f"pass={self.passed} scored={self.scored} cached={self.from_cache} "
            f"cost=${self.cost_usd:.4f}"
            + (" [budget reached]" if self.stopped_on_budget else "")
        )


def profile_from_row(row) -> UserProfile:
    return UserProfile.model_validate(
        {key: value for key, value in row._mapping.items() if key != "updated_at"}
    )


async def run_all(settings: Settings | None = None) -> list[MatchReport]:
    settings = settings or get_settings()
    async with connect() as conn:
        users = await users_q.list_users(conn)

    reports = []
    for user in users:
        if not user.is_active:
            continue
        try:
            reports.append(await run_for_user(user.id, settings))
        except Exception as exc:  # noqa: BLE001 - one user's failure is not the run's
            log.warning("matching failed for user %s: %s", user.id, exc, exc_info=True)
            reports.append(MatchReport(user_id=user.id, errors=[str(exc)]))
    return reports


async def run_for_user(user_id: int, settings: Settings | None = None) -> MatchReport:
    settings = settings or get_settings()
    report = MatchReport(user_id=user_id)

    async with connect() as conn:
        profile_row = await users_q.get_profile(conn, user_id)
        if profile_row is None:
            report.errors.append("This user has no profile row.")
            return report
        profile = profile_from_row(profile_row)
        credential = await users_q.get_credential(conn, user_id)
        queries = await _queries(conn, settings, profile, credential, report)

    if not queries:
        report.errors.append(
            "This profile yields no search queries; set at least a title or some keywords."
        )
        return report
    report.queries = len(queries)

    async with connect() as conn:
        await _retrieve(conn, profile, queries, report)
        await _apply_rules(conn, profile, report)

    if credential is not None:
        async with connect() as conn:
            await _rerank(conn, settings, profile, credential, report)
    log.info("match complete: %s", report.summary())
    return report


async def _queries(
    conn: AsyncConnection, settings: Settings, profile: UserProfile, credential, report
) -> list[str]:
    """The expanded query set for this profile version, computed at most once per version."""
    cached = await match_q.get_query_expansion(
        conn, profile.user_id, profile.version, QUERY_EXPANSION_VERSION
    )
    if cached is not None:
        return cached

    deterministic = expand.deterministic_queries(profile)
    generated: list[str] = []
    if credential is not None and deterministic:
        try:
            generated, usage = await expand.expand_with_model(
                settings,
                profile,
                api_key=decrypt(credential.api_key_encrypted),
                model=credential.model,
                provider_pin=credential.provider_pin,
            )
            await users_q.add_spend(
                conn, profile.user_id, tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out, cost_usd=usage.cost_usd,
            )
            report.cost_usd += usage.cost_usd
        except (llm.LlmError, CredentialError) as exc:
            # Expansion is an enhancement, never a prerequisite: retrieval must still work for a
            # user with no key, no credit or a key we can no longer decrypt.
            log.info("query expansion unavailable for user %s: %s", profile.user_id, exc)
            report.errors.append(f"Query expansion skipped: {exc}")

    combined = expand.combine(deterministic, generated)
    if combined:
        await match_q.put_query_expansion(
            conn, profile.user_id, profile.version, QUERY_EXPANSION_VERSION, combined
        )
    return combined


async def _retrieve(
    conn: AsyncConnection, profile: UserProfile, queries: list[str], report: MatchReport
) -> None:
    """Hybrid retrieval: one dense search per query, one lexical search per query, fused by rank."""
    arms = await retrieve.retrieve_arms(conn, profile, queries)
    fused = retrieve.fuse(arms, profile.retrieval_limit)
    dense_rank, lexical_rank = retrieve.ranks(arms)

    rows = [
        {
            "user_id": profile.user_id,
            "job_id": job_id,
            "profile_version": profile.version,
            "retrieval_score": score,
            "dense_rank": dense_rank.get(job_id),
            "lexical_rank": lexical_rank.get(job_id),
        }
        for job_id, score in fused
    ]
    await match_q.upsert_matches(conn, rows)
    report.retrieved = len(rows)


async def _apply_rules(
    conn: AsyncConnection, profile: UserProfile, report: MatchReport
) -> None:
    pending = await match_q.pending_rules(conn, profile.user_id, profile.retrieval_limit)
    verdicts = []
    for row in pending:
        candidate = Candidate(
            job_id=row.job_id,
            content_hash=row.content_hash,
            title=row.title,
            company=row.company,
            description=row.description,
        )
        verdict, reason = evaluate(candidate, profile)
        verdicts.append(
            {
                "user_id": profile.user_id,
                "job_id": row.job_id,
                "rule_verdict": verdict.value,
                "rule_reason": reason,
            }
        )
        if verdict is RuleVerdict.PASS:
            report.passed += 1
        else:
            report.rejected += 1
    await match_q.apply_rule_verdicts(conn, verdicts)


async def _rerank(
    conn: AsyncConnection, settings: Settings, profile: UserProfile, credential, report
) -> None:
    pending = await match_q.pending_rerank(
        conn, profile.user_id, profile.version, profile.rerank_limit
    )
    if not pending:
        return

    cached = await match_q.cached_scores(
        conn, profile.user_id, profile.version, [row.content_hash for row in pending]
    )
    uncached = []
    applied = []
    for row in pending:
        hit = cached.get(row.content_hash)
        if hit is None:
            uncached.append(row)
            continue
        applied.append(
            {
                "job_id": row.job_id, "score": hit.score, "reason": hit.reason,
                "red_flags": hit.red_flags, "profile_version": profile.version,
            }
        )
    if applied:
        await match_q.apply_scores(conn, profile.user_id, applied)
        report.from_cache = len(applied)

    if not uncached:
        return

    try:
        api_key = decrypt(credential.api_key_encrypted)
    except CredentialError as exc:
        report.errors.append(str(exc))
        return

    spend_row = await users_q.month_spend(conn, profile.user_id)
    spent = Decimal(spend_row.cost_usd) if spend_row else Decimal(0)
    budget = Decimal(credential.monthly_budget_usd)
    # Before any batch has run there is no measured cost, so the first check uses a deliberately
    # pessimistic figure rather than zero -- a zero estimate would always pass the ceiling test.
    estimate = Decimal("0.02")

    for start in range(0, len(uncached), rerank.BATCH_SIZE):
        if rerank.would_exceed_budget(spent, budget, estimate):
            report.stopped_on_budget = True
            log.info(
                "user %s reached the monthly ceiling ($%.2f of $%.2f); %d jobs left unscored",
                profile.user_id, spent, budget, len(uncached) - start,
            )
            break

        batch = uncached[start : start + rerank.BATCH_SIZE]
        try:
            scores, usage = await rerank.score_batch(
                settings, profile, batch,
                api_key=api_key, model=credential.model,
                provider_pin=credential.provider_pin,
            )
        except llm.LlmError as exc:
            report.errors.append(str(exc))
            log.warning("rerank batch failed for user %s: %s", profile.user_id, exc)
            break

        spent = await users_q.add_spend(
            conn, profile.user_id, tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out, cost_usd=usage.cost_usd,
        )
        report.cost_usd += usage.cost_usd
        if usage.cost_usd > 0:
            estimate = usage.cost_usd

        by_id = {row.job_id: row for row in batch}
        updates, cache_rows = [], []
        for score in scores:
            row = by_id.get(score.id)
            if row is None:
                continue
            updates.append(
                {
                    "job_id": row.job_id, "score": score.score, "reason": score.reason,
                    "red_flags": score.red_flags, "profile_version": profile.version,
                }
            )
            cache_rows.append(
                {
                    "content_hash": row.content_hash, "user_id": profile.user_id,
                    "profile_version": profile.version, "score": score.score,
                    "reason": score.reason, "red_flags": score.red_flags,
                    "model": credential.model,
                }
            )
        await match_q.apply_scores(conn, profile.user_id, updates)
        await match_q.put_cached_scores(conn, cache_rows)
        report.scored += len(updates)
