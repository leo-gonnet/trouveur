"""Per-user matching: retrieve, then rerank. Orchestration only.

Runs per user in ISOLATION: one user's expired key or exhausted budget must never affect
another's results. Retrieval is free and re-runnable; only the last stage costs money.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    stopped_on_scores: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"user={self.user_id} queries={self.queries} retrieved={self.retrieved} "
            f"scored={self.scored} cached={self.from_cache} "
            f"published={self.published} "
            f"cost=${self.cost_usd:.4f}"
            + (" [budget reached]" if self.stopped_on_budget else "")
            + (" [scores ran out]" if self.stopped_on_scores else "")
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


async def run_for_user(
    user_id: int, settings: Settings | None = None, *, whole_horizon: bool = False
) -> MatchReport:
    """One user's run.

    `whole_horizon` widens the candidate window from the last sweep's additions to everything
    still retained. The daily run after a sweep leaves it off: a posting gets one chance, on the
    day it arrives. A run the USER caused -- a profile change, a key added, scoring switched back
    on -- turns it on, because in each of those cases nothing has been judged under the terms that
    now apply and the last seven days deserve a fresh look.
    """
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

    # One pair of cutoffs for the whole run, so the two arms cannot disagree about either.
    fresh_since = freshness.fresh_since(settings.retrieval_horizon_days)
    seen_since = (
        fresh_since
        if whole_horizon
        else datetime.now(UTC) - timedelta(hours=retrieve.NEW_ARRIVALS_HOURS)
    )
    async with connect() as conn:
        retrieved = await _retrieve(
            conn, profile, queries, report, adverts,
            fresh_since=fresh_since, seen_since=seen_since,
        )

    if credential is not None and retrieved:
        async with connect() as conn:
            await _rerank(
                conn, settings, profile, credential, report, retrieved,
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
    seen_since: datetime,
) -> list[int]:
    """Hybrid retrieval: dense searches per query and advert, lexical per query, fused by rank."""
    arms = await retrieve.retrieve_arms(
        conn, profile, queries, adverts or [],
        fresh_since=fresh_since, seen_since=seen_since,
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
    # Handed to the scorer, which scores what THIS run found and nothing else.
    return [job_id for job_id, _ in fused]


async def _rerank(
    conn: AsyncConnection,
    settings: Settings,
    profile: UserProfile,
    credential,
    report,
    retrieved: list[int],
    *,
    background: str = "",
) -> None:
    """Score what is pending, then publish the day's edition from whatever was scored."""
    scored = await _score_pending(
        conn, settings, profile, credential, report, retrieved, background=background
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
    retrieved: list[int],
    *,
    background: str = "",
) -> list[dict]:
    """Every posting this run put a score on, from the cache or from the model.

    Returned rather than published here, so that a run cut short by the ceiling, a bad key or a
    failed batch still publishes what it did manage to score.
    """
    scored: list[dict] = []
    pending = await match_q.pending_rerank(
        conn, profile.user_id, profile.version, retrieved
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
    estimate = Decimal("0.002")
    failures: list[str] = []
    weak_blocks = 0
    gate = asyncio.Semaphore(rerank.CONCURRENCY)

    async def attempt(row):
        """One posting's call, with the one failure the caller can do something about caught.

        Returned rather than raised so a block survives a single timeout: at a few thousand calls
        a run, ending the whole stage on the first blip would leave most of the corpus unscored.
        Only LlmError is caught -- anything else is a bug and must not be swallowed per posting.
        """
        async with gate:
            try:
                return await rerank.score_one(
                    settings, profile, row,
                    api_key=api_key, model=settings.default_llm_model,
                    provider_pin=settings.default_llm_provider,
                    background=background,
                )
            except llm.LlmError as exc:
                return exc

    for start in range(0, len(uncached), rerank.BLOCK):
        block = uncached[start : start + rerank.BLOCK]
        # Before the block goes out, and priced for the whole block: its calls are issued
        # together, so the ceiling has to be tested against what all of them can cost. Never
        # after -- a retry loop on someone else's card is not something to discover from the user.
        if rerank.would_exceed_budget(spent, budget, estimate * len(block)):
            report.stopped_on_budget = True
            log.info(
                "user %s reached the monthly ceiling ($%.2f of $%.2f); %d jobs left unscored",
                profile.user_id, spent, budget, len(uncached) - start,
            )
            break

        results = await asyncio.gather(*(attempt(row) for row in block))

        updates, cache_rows = [], []
        tokens_in = tokens_out = 0
        cost = Decimal(0)
        for row, outcome in zip(block, results, strict=True):
            if isinstance(outcome, llm.LlmError):
                failures.append(str(outcome))
                continue
            score, usage = outcome
            tokens_in += usage.tokens_in
            tokens_out += usage.tokens_out
            cost += usage.cost_usd
            estimate = max(estimate, usage.cost_usd)
            # A response that could not be parsed is billed and left unscored, never cached and
            # never recorded as a zero: the next run asks again.
            if score is None:
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

        if tokens_in or tokens_out or cost:
            spent = await users_q.add_spend(
                conn, profile.user_id,
                tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost,
            )
        report.cost_usd += cost
        await match_q.apply_scores(conn, profile.user_id, updates)
        await match_q.put_cached_scores(conn, cache_rows)
        report.scored += len(updates)
        scored.extend(updates)

        # Every call in the block failed: that is the key, the credit or the provider, not a
        # blip, and the next block would fail the same way on the user's own card.
        if all(isinstance(outcome, llm.LlmError) for outcome in results):
            log.warning("every scoring call failed for user %s; stopping", profile.user_id)
            break

        # The scores have run out. Judged on the median of the block rather than on any one
        # posting, and only after two blocks in a row, because retrieval order correlates loosely
        # with the model's verdict and one weak block is noise. Nothing is hidden by this: what
        # was scored is published whatever it scored -- the rule decides when to stop BUYING.
        # An empty block counts as weak: every call was billed and none came back parseable,
        # which is the model not returning JSON rather than a quiet day, and no reason to buy more.
        block_scores = [row["score"] for row in updates]
        if not block_scores or statistics.median(block_scores) < rerank.SCORE_FLOOR:
            weak_blocks += 1
            if weak_blocks >= rerank.WEAK_BLOCKS_BEFORE_STOPPING:
                report.stopped_on_scores = True
                log.info(
                    "user %s: scores ran out after %d postings; %d left unscored",
                    profile.user_id, start + len(block), len(uncached) - start - len(block),
                )
                break
        else:
            weak_blocks = 0

    if failures:
        report.errors.append(f"{len(failures)} scoring call(s) failed: {failures[0]}")

    return scored
