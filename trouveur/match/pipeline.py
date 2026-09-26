"""Per-user matching: retrieve, then rerank. Orchestration only.

Runs per user in ISOLATION: one user's expired key or exhausted budget must never affect
another's results. Retrieval is free and re-runnable; only the last stage costs money.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur import clock
from trouveur.config import Settings, get_settings
from trouveur.crypto import CredentialError, decrypt
from trouveur.db.engine import connect
from trouveur.db.queries import freshness
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.match import expand, llm, rerank, retrieve
from trouveur.models import Expansion, UserProfile
from trouveur.versions import QUERY_EXPANSION_VERSION

log = logging.getLogger(__name__)


@dataclass
class MatchReport:
    user_id: int
    queries: int = 0
    retrieved: int = 0
    scored: int = 0
    from_cache: int = 0
    published: int = 0
    cost_usd: Decimal = Decimal(0)
    stopped_on_budget: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"user={self.user_id} queries={self.queries} retrieved={self.retrieved} "
            f"scored={self.scored} cached={self.from_cache} "
            f"published={self.published} "
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
        if not profile.scoring_enabled:
            # The user's pause. Every paid path in this module is already gated on a credential,
            # so dropping it here stops scoring AND the billed query expansion in one place.
            # Retrieval is free and still runs, which is what keeps Search working while paused.
            credential = None
        expansion = await _queries(conn, settings, profile, credential, report)

    queries, adverts = expansion.queries, expansion.adverts
    if not queries:
        report.errors.append(
            "This profile yields no search queries; set at least a title or some keywords."
        )
        return report
    report.queries = len(queries) + len(adverts)

    # One cutoff for the whole run, so the two arms cannot disagree about what is fresh.
    fresh_since = freshness.fresh_since(settings.retrieval_horizon_days)
    async with connect() as conn:
        await _retrieve(conn, profile, queries, report, adverts, fresh_since=fresh_since)

    if credential is not None:
        async with connect() as conn:
            await _rerank(
                conn, settings, profile, credential, report,
                background=expansion.background_summary or expand.truncated_background(profile),
            )
    log.info("match complete: %s", report.summary())
    return report


async def _queries(
    conn: AsyncConnection, settings: Settings, profile: UserProfile, credential, report
) -> Expansion:
    """The expansion for this profile version, computed at most once per version."""
    cached = await match_q.get_query_expansion(
        conn, profile.user_id, profile.version, QUERY_EXPANSION_VERSION
    )
    if cached is not None:
        return cached

    deterministic = expand.deterministic_queries(profile)
    generated: list[str] = []
    adverts: list[str] = []
    summary = ""
    if credential is not None and deterministic:
        try:
            generated, usage = await expand.expand_with_model(
                settings,
                profile,
                api_key=decrypt(credential.api_key_encrypted),
                model=settings.default_llm_model,
                provider_pin=settings.default_llm_provider,
            )
            await users_q.add_spend(
                conn, profile.user_id, tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out, cost_usd=usage.cost_usd,
            )
            report.cost_usd += usage.cost_usd
        except (llm.LlmError, CredentialError) as exc:
            # An enhancement, never a prerequisite: retrieval must work with no key at all.
            log.info("query expansion unavailable for user %s: %s", profile.user_id, exc)
            report.errors.append(f"Query expansion skipped: {exc}")

        # Separately survivable: losing the long output must not also lose the phrases.
        try:
            adverts, advert_usage = await expand.expand_adverts(
                settings,
                profile,
                api_key=decrypt(credential.api_key_encrypted),
                model=settings.default_llm_model,
                provider_pin=settings.default_llm_provider,
            )
            await users_q.add_spend(
                conn, profile.user_id, tokens_in=advert_usage.tokens_in,
                tokens_out=advert_usage.tokens_out, cost_usd=advert_usage.cost_usd,
            )
            report.cost_usd += advert_usage.cost_usd
        except (llm.LlmError, CredentialError) as exc:
            log.info("advert expansion unavailable for user %s: %s", profile.user_id, exc)
            report.errors.append(f"Advert expansion skipped: {exc}")

        # Distilled here rather than sent raw with every batch: one cost per profile version
        # instead of one per ten postings.
        if profile.background.strip():
            try:
                summary, summary_usage = await expand.summarise_background(
                    settings,
                    profile,
                    api_key=decrypt(credential.api_key_encrypted),
                    model=settings.default_llm_model,
                    provider_pin=settings.default_llm_provider,
                )
                await users_q.add_spend(
                    conn, profile.user_id, tokens_in=summary_usage.tokens_in,
                    tokens_out=summary_usage.tokens_out, cost_usd=summary_usage.cost_usd,
                )
                report.cost_usd += summary_usage.cost_usd
            except (llm.LlmError, CredentialError) as exc:
                log.info("background summary unavailable for user %s: %s", profile.user_id, exc)
                report.errors.append(f"Background summary skipped: {exc}")

    combined = expand.combine(deterministic, generated)
    expansion = Expansion(queries=combined, adverts=adverts, background_summary=summary)
    # Only a full expansion is worth keeping. Caching the deterministic floor -- which is what a
    # run with no key, or with scoring paused, produces -- would pin this profile version to it
    # for ever, so adding a key later would silently buy nothing.
    if combined and credential is not None:
        await match_q.put_query_expansion(
            conn, profile.user_id, profile.version, QUERY_EXPANSION_VERSION, expansion
        )
    return expansion


async def _retrieve(
    conn: AsyncConnection,
    profile: UserProfile,
    queries: list[str],
    report: MatchReport,
    adverts: list[str] | None = None,
    *,
    fresh_since: datetime,
) -> None:
    """Hybrid retrieval: dense searches per query and advert, lexical per query, fused by rank."""
    arms = await retrieve.retrieve_arms(
        conn, profile, queries, adverts or [], fresh_since=fresh_since
    )
    fused = retrieve.fuse(arms, retrieve.FUSED_LIMIT)
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


async def _rerank(
    conn: AsyncConnection,
    settings: Settings,
    profile: UserProfile,
    credential,
    report,
    *,
    background: str = "",
) -> None:
    """Score what is pending, then publish the day's edition from whatever was scored."""
    scored = await _score_pending(
        conn, settings, profile, credential, report, background=background
    )
    if not scored:
        return
    await _publish(conn, profile, report, scored)


async def _publish(
    conn: AsyncConnection, profile: UserProfile, report, scored: list[dict]
) -> None:
    """Write today's edition.

    The clear and the insert share this transaction on purpose: a run that dies between them
    would otherwise delete a published day and put nothing back in its place.
    """
    # The reader's day, not the server's: an edition published at 01:00 in Vienna belongs to
    # that morning's reading, not to the day UTC was still on. Decided here and stored, because
    # the page must not recompute a published day.
    day = clock.today()
    await match_q.clear_stale_edition(conn, profile.user_id, day, profile.version)
    report.published = await match_q.publish_edition(
        conn,
        [
            {
                "user_id": profile.user_id,
                "day": day,
                "job_id": row["job_id"],
                "profile_version": profile.version,
                "llm_score": row["score"],
                "llm_reason": row["reason"],
                "llm_red_flags": row["red_flags"],
            }
            for row in scored
        ],
    )


async def _score_pending(
    conn: AsyncConnection,
    settings: Settings,
    profile: UserProfile,
    credential,
    report,
    *,
    background: str = "",
) -> list[dict]:
    """Every posting this run put a score on, from the cache or from the model.

    Returned rather than published here, so that a run cut short by the ceiling, a bad key or a
    failed batch still publishes what it did manage to score.
    """
    scored: list[dict] = []
    pending = await match_q.pending_rerank(
        conn, profile.user_id, profile.version, rerank.RERANK_LIMIT
    )
    if not pending:
        return scored

    cached = await match_q.cached_scores(
        conn, profile.user_id, profile.version, [row.content_hash for row in pending]
    )
    uncached = []
    for row in pending:
        hit = cached.get(row.content_hash)
        if hit is None:
            uncached.append(row)
            continue
        scored.append(
            {
                "job_id": row.job_id, "score": hit.score, "reason": hit.reason,
                "red_flags": hit.red_flags, "profile_version": profile.version,
            }
        )
    if scored:
        await match_q.apply_scores(conn, profile.user_id, scored)
        report.from_cache = len(scored)

    if not uncached:
        return scored

    try:
        api_key = decrypt(credential.api_key_encrypted)
    except CredentialError as exc:
        report.errors.append(str(exc))
        return scored

    spend_row = await users_q.month_spend(conn, profile.user_id)
    spent = Decimal(spend_row.cost_usd) if spend_row else Decimal(0)
    budget = Decimal(credential.monthly_budget_usd)
    # Pessimistic, not zero: a zero estimate would always pass the ceiling test.
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
                api_key=api_key, model=settings.default_llm_model,
                provider_pin=settings.default_llm_provider,
                background=background,
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
                    "model": settings.default_llm_model,
                }
            )
        await match_q.apply_scores(conn, profile.user_id, updates)
        await match_q.put_cached_scores(conn, cache_rows)
        report.scored += len(updates)
        scored.extend(updates)

    return scored
