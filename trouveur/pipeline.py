"""Orchestration only: no parsing, no SQL (see AGENTS.md).

    collect -> rules(1) -> enrich -> rules(2) -> llm -> notify

Two rules passes are deliberate. Search results carry no description and fetching one costs a
request per job, so the cheap signals (title, location, salary) cull the set first and only
survivors are enriched. The second pass applies the rules that need a description, such as
deal-breaker phrases and the staffing-agency flag.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from trouveur.config import Settings, get_settings
from trouveur.db import queries as q
from trouveur.db.engine import connect
from trouveur.filters import llm as llm_filter
from trouveur.filters.rules import evaluate, title_is_plausible
from trouveur.models import Job, ProfileData, RuleVerdict
from trouveur.notify import email
from trouveur.sources import SOURCE_NAMES
from trouveur.sources.arbeitsagentur import ArbeitsagenturSource
from trouveur.sources.http import PoliteClient
from trouveur.sources.jobspy_source import JobSpySource
from trouveur.sources.karriere_at import KarriereAtSource
from trouveur.sources.personio import PersonioSource

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    collected: int = 0
    new: int = 0
    rules_passed: int = 0
    rules_rejected: int = 0
    enriched: int = 0
    scored: int = 0
    notified: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    per_source: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [
            f"collected={self.collected}", f"new={self.new}",
            f"rules_pass={self.rules_passed}", f"rules_reject={self.rules_rejected}",
            f"enriched={self.enriched}", f"scored={self.scored}", f"notified={self.notified}",
        ]
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


def build_sources(
    profile: ProfileData,
    enabled: Collection[str],
    tenants: list[str] | None = None,
    only: str | None = None,
) -> list:
    """Build the adapters the user has activated, in run order.

    `enabled` has no default: a source runs only when it is switched on in the web UI, and a
    caller that forgets to pass the activated set must fail loudly rather than quietly scanning
    everything.
    """
    keywords = profile.keywords or [profile.title or "Wirtschaftsingenieur"]
    sources = [
        ArbeitsagenturSource(keywords=keywords),
        # karriere.at renders titles in its listings, so reject obvious misses before paying for
        # a detail page. Recall there scales with keyword count, not page depth: the search has
        # no working pagination.
        KarriereAtSource(
            keywords=keywords,
            title_filter=lambda title: title_is_plausible(title, profile),
        ),
    ]

    # Only ever sees the tenants stored in the database; there is no global Personio index.
    if tenants:
        sources.append(PersonioSource(tenants=tenants))

    # Runs last and narrow: LinkedIn rate-limits around page 10 from a single IP.
    sources.append(JobSpySource(keywords=keywords, countries=profile.countries))

    activated = set(enabled)
    sources = [s for s in sources if s.name in activated]

    if only:
        if only not in SOURCE_NAMES:
            raise SystemExit(f"unknown source: {only}")
        if only not in activated:
            raise SystemExit(
                f"source {only} is not activated; switch it on under Sources in the web UI"
            )
        sources = [s for s in sources if s.name == only]
        if not sources:
            # Activated but nothing to scan: today that is only Personio without companies.
            raise SystemExit(f"source {only} is activated but has nothing configured to scan")
    return sources


async def collect(
    sources: list, since: datetime | None, report: RunReport
) -> list[tuple[object, Job]]:
    """Fetch from every source. One failing source is a logged warning, never a crash."""
    collected: list[tuple[object, Job]] = []
    async with PoliteClient() as client:
        for source in sources:
            count = 0
            try:
                async for job in source.fetch(client, since):
                    collected.append((source, job))
                    count += 1
                log.info("source %s yielded %d jobs", source.name, count)
            except Exception as exc:  # noqa: BLE001 - per-source isolation, see AGENTS.md
                log.warning("source %s failed: %s", source.name, exc, exc_info=True)
                report.errors[source.name] = str(exc)
            report.per_source[source.name] = count
    report.collected = len(collected)
    return collected


async def _record_source_health(conn, sources: list, report: RunReport) -> None:
    """Persist per-source outcome for the dashboard's health panel.

    One row per configured source per run, win or lose, so a source that starts failing shows up
    as unhealthy rather than simply vanishing from the numbers.
    """
    for source in sources:
        error = report.errors.get(source.name)
        await q.record_source_run(
            conn, source.name, ok=error is None,
            jobs_seen=report.per_source.get(source.name, 0), error=error,
        )


async def run(
    *, since_days: int = 1, only_source: str | None = None, dry_run: bool = False,
    use_llm: bool = True, send_email: bool = True, settings: Settings | None = None,
) -> RunReport:
    settings = settings or get_settings()
    report = RunReport()
    since = datetime.now(UTC) - timedelta(days=since_days)

    profile, tenants, enabled = await _load_run_config(dry_run)
    sources = build_sources(profile, enabled, tenants, only_source)
    if not sources:
        # A run with nothing activated collects nothing and would otherwise look like a healthy
        # empty day. Say so.
        log.warning("no sources are activated; activate one under Sources in the web UI")
    collected = await collect(sources, since, report)

    if dry_run:
        _report_dry_run(collected, profile, report)
        return report

    async with connect() as conn:
        await _record_source_health(conn, sources, report)

        job_ids: list[tuple[int, Job, object]] = []
        for source, job in collected:
            job_id, was_new = await q.upsert_job(conn, job)
            if job_id > 0:
                job_ids.append((job_id, job, source))
                report.new += int(was_new)

        # Rules pass 1: cheap signals only, no description yet.
        survivors = []
        for job_id, job, source in job_ids:
            verdict, reason = evaluate(job, profile)
            await q.set_rule_verdict(conn, job_id, verdict)
            if verdict == RuleVerdict.PASS:
                survivors.append((job_id, job, source))
            else:
                report.rules_rejected += 1
                log.debug("rejected %s: %s", job.title, reason)

        # Enrich only survivors, then re-apply the rules that need a description.
        async with PoliteClient() as client:
            for job_id, job, source in list(survivors):
                if not hasattr(source, "enrich"):
                    continue
                try:
                    extra = await source.enrich(client, job.source_native_id)
                except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
                    log.warning("enrich failed for %s: %s", job.source_native_id, exc)
                    continue
                report.enriched += 1
                job.description = extra.get("description") or job.description
                job.raw.update(
                    {
                        "istArbeitnehmerUeberlassung": extra.get("is_staffing_agency"),
                        "istPrivateArbeitsvermittlung": extra.get("is_private_agency"),
                    }
                )
                await q.upsert_job(conn, job)

                verdict, reason = evaluate(job, profile)
                if verdict != RuleVerdict.PASS:
                    await q.set_rule_verdict(conn, job_id, verdict)
                    survivors.remove((job_id, job, source))
                    report.rules_rejected += 1
                    log.debug("rejected after enrich %s: %s", job.title, reason)

        report.rules_passed = len(survivors)

        if use_llm:
            report.scored = await _score(conn, settings, profile)

        if send_email:
            report.notified = await _notify(conn, settings, profile)

    log.info("run complete: %s", report.summary())
    return report


async def _score(conn, settings: Settings, profile: ProfileData) -> int:
    pending = await q.jobs_awaiting_llm(conn)
    if not pending:
        return 0

    uncached = []
    for row in pending:
        cached = await q.get_cached_score(conn, row.content_hash, profile.version)
        if cached is not None:
            await q.apply_score(conn, row.id, cached.score, cached.reason, cached.red_flags)
        else:
            uncached.append(row)

    scored = 0
    for start in range(0, len(uncached), llm_filter.BATCH_SIZE):
        batch = uncached[start : start + llm_filter.BATCH_SIZE]
        by_id = {row.id: row for row in batch}
        try:
            results = await llm_filter.score_batch(settings, profile, batch)
        except Exception as exc:  # noqa: BLE001 - a scoring failure must not lose the run
            log.warning("LLM batch failed: %s", exc)
            continue
        for result in results:
            row = by_id.get(result.id)
            if row is None:
                continue
            await q.apply_score(conn, row.id, result.score, result.reason, result.red_flags)
            await q.put_cached_score(
                conn, row.content_hash, profile.version, result.score,
                result.reason, result.red_flags, settings.llm_model,
            )
            scored += 1
    return scored


async def _notify(conn, settings: Settings, profile: ProfileData) -> int:
    rows = await q.jobs_to_notify(conn, profile.notify_threshold)
    if not rows:
        log.info("nothing above threshold %d to notify", profile.notify_threshold)
        return 0
    subject, text, html = email.render(rows, profile.notify_threshold)
    email.send(settings, subject, text, html)
    await q.mark_notified(conn, [row.id for row in rows])
    return len(rows)


async def _load_run_config(dry_run: bool) -> tuple[ProfileData, list[str], list[str]]:
    if dry_run:
        # Dry runs must work before Postgres exists, so fall back to defaults.
        try:
            async with connect() as conn:
                row = await q.get_profile(conn)
                tenants = await q.enabled_personio_tenants(conn)
                enabled = await q.enabled_sources(conn)
                return (_row_to_profile(row) if row else ProfileData()), tenants, enabled
        except Exception as exc:  # noqa: BLE001
            log.info("no database available (%s); using default profile for dry run", exc)
            return ProfileData(), [], []
    async with connect() as conn:
        await q.ensure_profile(conn)
        row = await q.get_profile(conn)
        tenants = await q.enabled_personio_tenants(conn)
        enabled = await q.enabled_sources(conn)
        return (_row_to_profile(row) if row else ProfileData()), tenants, enabled


def _row_to_profile(row) -> ProfileData:
    return ProfileData.model_validate({k: v for k, v in row._mapping.items() if k != "id"})


def _report_dry_run(collected: list, profile: ProfileData, report: RunReport) -> None:
    for _source, job in collected:
        verdict, reason = evaluate(job, profile)
        if verdict == RuleVerdict.PASS:
            report.rules_passed += 1
        else:
            report.rules_rejected += 1
        where = ", ".join(filter(None, [job.location_city, job.location_country])) or "n/a"
        mark = "PASS" if verdict == RuleVerdict.PASS else "rjct"
        print(f"[{mark}] {job.title[:60]:<60} {(job.company or '')[:28]:<28} {where:<22} {reason}")
