"""All SQL for the project. No SQL text may live outside this package (see AGENTS.md)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.schema import (
    app_user,
    job,
    llm_score_cache,
    personio_tenant,
    pipeline_run,
    pipeline_schedule,
    profile,
    source_state,
)
from trouveur.models import Job, RuleVerdict, UserState, fold

# Columns refreshed when we see an existing posting again. Deliberately excludes first_seen,
# user_state and notified_at: those record what WE did, not what the source says.
_REFRESHABLE = (
    "url", "title", "company", "location_city", "location_country", "remote",
    "salary_min", "salary_max", "salary_period", "posted_at", "description", "raw",
)


async def upsert_job(conn: AsyncConnection, item: Job) -> tuple[int, bool]:
    """Insert or refresh one posting. Returns (job_id, was_new).

    Matches on either identity: the source's own id, or content_hash so the same vacancy arriving
    from a second aggregator collapses onto the existing row rather than duplicating.
    """
    existing = (
        await conn.execute(
            sa.select(job.c.id).where(
                sa.or_(
                    job.c.content_hash == item.content_hash,
                    sa.and_(
                        job.c.source == item.source,
                        job.c.source_native_id == item.source_native_id,
                    ),
                )
            ).limit(1)
        )
    ).scalar_one_or_none()

    values: dict[str, Any] = {
        "source": item.source,
        "source_native_id": item.source_native_id,
        "url": item.url,
        "title": item.title,
        "company": item.company,
        "location_city": item.location_city,
        "location_country": item.location_country,
        "remote": item.remote,
        "salary_min": item.salary_min,
        "salary_max": item.salary_max,
        "salary_period": item.salary_period.value,
        "posted_at": item.posted_at,
        "description": item.description,
        "raw": item.raw,
        "content_hash": item.content_hash,
        "search_norm": item.search_norm,
    }

    if existing is not None:
        await conn.execute(
            job.update()
            .where(job.c.id == existing)
            .values(last_seen=sa.func.now(), search_norm=item.search_norm,
                    **{k: values[k] for k in _REFRESHABLE})
        )
        return existing, False

    stmt = (
        pg_insert(job)
        .values(**values)
        .on_conflict_do_nothing(constraint="job_content_hash_uniq")
        .returning(job.c.id)
    )
    new_id = (await conn.execute(stmt)).scalar_one_or_none()
    return (new_id, True) if new_id is not None else (-1, False)


async def set_rule_verdict(conn: AsyncConnection, job_id: int, verdict: RuleVerdict) -> None:
    await conn.execute(job.update().where(job.c.id == job_id).values(rule_verdict=verdict.value))


async def jobs_awaiting_llm(conn: AsyncConnection, limit: int = 500) -> list[sa.Row]:
    return list(
        await conn.execute(
            sa.select(job.c.id, job.c.content_hash, job.c.title, job.c.company,
                      job.c.location_city, job.c.location_country, job.c.remote,
                      job.c.salary_min, job.c.salary_max, job.c.description)
            .where(job.c.rule_verdict == RuleVerdict.PASS.value, job.c.llm_score.is_(None))
            .order_by(job.c.posted_at.desc().nullslast())
            .limit(limit)
        )
    )


async def get_cached_score(
    conn: AsyncConnection, content_hash: bytes, profile_version: int
) -> sa.Row | None:
    return (
        await conn.execute(
            sa.select(llm_score_cache).where(
                llm_score_cache.c.content_hash == content_hash,
                llm_score_cache.c.profile_version == profile_version,
            )
        )
    ).one_or_none()


async def put_cached_score(
    conn: AsyncConnection, content_hash: bytes, profile_version: int,
    score: int, reason: str, red_flags: list[str], model: str,
) -> None:
    await conn.execute(
        pg_insert(llm_score_cache)
        .values(content_hash=content_hash, profile_version=profile_version, score=score,
                reason=reason, red_flags=red_flags, model=model)
        .on_conflict_do_update(
            index_elements=[llm_score_cache.c.content_hash],
            set_={"score": score, "reason": reason, "red_flags": red_flags,
                  "profile_version": profile_version, "model": model},
        )
    )


async def apply_score(
    conn: AsyncConnection, job_id: int, score: int, reason: str, red_flags: list[str]
) -> None:
    await conn.execute(
        job.update().where(job.c.id == job_id)
        .values(llm_score=score, llm_reason=reason, llm_red_flags=red_flags)
    )


async def jobs_to_notify(conn: AsyncConnection, threshold: int) -> list[sa.Row]:
    return list(
        await conn.execute(
            sa.select(job)
            .where(job.c.llm_score >= threshold, job.c.notified_at.is_(None))
            .order_by(job.c.llm_score.desc())
        )
    )


async def mark_notified(conn: AsyncConnection, job_ids: list[int]) -> None:
    if not job_ids:
        return
    await conn.execute(
        job.update().where(job.c.id.in_(job_ids)).values(notified_at=sa.func.now())
    )


async def search_jobs(
    conn: AsyncConnection, query: str = "", *, state: str | None = None,
    country: str | None = None, remote: bool | None = None, limit: int = 100,
) -> list[sa.Row]:
    """Full-text search across both German indexes.

    Two indexes are queried on purpose (see AGENTS.md > Database rules): the `german` tsvector
    handles inflection (Ingenieure -> Ingenieur), while the trigram index on the folded column is
    what matches inside compounds (ingenieur inside Wirtschaftsingenieur). Neither alone suffices.
    """
    stmt = sa.select(job)
    if query.strip():
        folded = fold(query)
        # Escape LIKE wildcards so a literal % or _ typed by the user is not a pattern.
        pattern = "%" + folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            sa.or_(
                sa.text("search_de @@ websearch_to_tsquery('german', :q)").bindparams(q=query),
                job.c.search_norm.like(pattern, escape="\\"),
            )
        ).order_by(
            sa.desc(sa.text("ts_rank_cd(search_de, websearch_to_tsquery('german', :q2))")
                    .bindparams(q2=query)),
            sa.desc(sa.func.similarity(job.c.search_norm, folded)),
        )
    else:
        stmt = stmt.order_by(job.c.llm_score.desc().nullslast(),
                             job.c.posted_at.desc().nullslast())
    if state:
        stmt = stmt.where(job.c.user_state == state)
    if country:
        stmt = stmt.where(job.c.location_country == country)
    if remote is not None:
        stmt = stmt.where(job.c.remote.is_(remote))
    return list(await conn.execute(stmt.limit(limit)))


async def recommendations(conn: AsyncConnection, threshold: int, limit: int = 50) -> list[sa.Row]:
    """The strict, small list: jobs that passed both filter stages and cleared the threshold.

    Deliberately excludes unscored jobs (rule_verdict='pass' but llm_score IS NULL is the normal
    state whenever the LLM stage hasn't run yet -- no API key, --no-llm, or a pending batch). This
    page is meant to be short; everything scraped, filtered or not, lives in /search instead.
    """
    return list(
        await conn.execute(
            sa.select(job)
            .where(
                job.c.user_state == UserState.NEW.value,
                job.c.rule_verdict == RuleVerdict.PASS.value,
                job.c.llm_score.is_not(None),
                job.c.llm_score >= threshold,
            )
            .order_by(job.c.llm_score.desc(), job.c.posted_at.desc().nullslast())
            .limit(limit)
        )
    )


async def set_user_state(conn: AsyncConnection, job_id: int, state: UserState) -> None:
    await conn.execute(job.update().where(job.c.id == job_id).values(user_state=state.value))


async def get_profile(conn: AsyncConnection) -> sa.Row | None:
    return (await conn.execute(sa.select(profile).where(profile.c.id == 1))).one_or_none()


async def ensure_profile(conn: AsyncConnection) -> None:
    await conn.execute(pg_insert(profile).values(id=1).on_conflict_do_nothing())


async def save_profile(conn: AsyncConnection, values: dict[str, Any]) -> None:
    """Persist the profile and bump its version.

    The version bump is what invalidates llm_score_cache: cached scores were judged against the
    old objectives, so they are no longer valid answers.
    """
    await conn.execute(
        profile.update().where(profile.c.id == 1)
        .values(updated_at=sa.func.now(), version=profile.c.version + 1, **values)
    )
    await conn.execute(job.update().values(llm_score=None, llm_reason=None, llm_red_flags=None))


async def get_user(conn: AsyncConnection) -> sa.Row | None:
    return (await conn.execute(sa.select(app_user).where(app_user.c.id == 1))).one_or_none()


async def create_user(conn: AsyncConnection, username: str, password_hash: str) -> None:
    await conn.execute(
        pg_insert(app_user)
        .values(id=1, username=username, password_hash=password_hash)
        .on_conflict_do_update(index_elements=[app_user.c.id],
                               set_={"username": username, "password_hash": password_hash})
    )


async def record_login_result(
    conn: AsyncConnection, ok: bool, locked_until: datetime | None
) -> None:
    if ok:
        values: dict[str, Any] = {"failed_attempts": 0, "locked_until": None}
    else:
        values = {
            "failed_attempts": app_user.c.failed_attempts + 1,
            "locked_until": locked_until,
        }
    await conn.execute(app_user.update().where(app_user.c.id == 1).values(**values))


async def get_source_state(conn: AsyncConnection, source: str) -> sa.Row | None:
    return (
        await conn.execute(sa.select(source_state).where(source_state.c.source == source))
    ).one_or_none()


async def record_source_run(
    conn: AsyncConnection, source: str, *, ok: bool, jobs_seen: int = 0,
    error: str | None = None, cursor: str | None = None, etag: str | None = None,
) -> None:
    values: dict[str, Any] = {
        "source": source, "last_run_at": sa.func.now(),
        "last_error": error, "jobs_seen": jobs_seen,
    }
    if ok:
        values["last_success_at"] = sa.func.now()
    if cursor is not None:
        values["cursor"] = cursor
    if etag is not None:
        values["etag"] = etag
    await conn.execute(
        pg_insert(source_state).values(**values).on_conflict_do_update(
            index_elements=[source_state.c.source],
            set_={k: v for k, v in values.items() if k != "source"},
        )
    )


async def daily_intake(conn: AsyncConnection, days: int = 30) -> list[sa.Row]:
    """New jobs per day per source, keyed on first_seen (when we discovered it, not when it was
    posted) so the chart reflects pipeline activity rather than employer behaviour."""
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT date_trunc('day', first_seen)::date AS day,
                       source,
                       count(*) AS n
                FROM job
                WHERE first_seen >= now() - make_interval(days => :days)
                GROUP BY 1, 2
                ORDER BY 1, 2
                """
            ).bindparams(days=days)
        )
    )


async def source_quality(conn: AsyncConnection, threshold: int) -> list[sa.Row]:
    """Per-source funnel: scraped -> passed rules -> scored -> recommended.

    `match_rate` is recommended/scraped, which is the number that says whether a source is worth
    its request budget.
    """
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT source,
                       count(*)                                              AS scraped,
                       count(*) FILTER (WHERE rule_verdict = 'pass')         AS passed_rules,
                       count(*) FILTER (WHERE llm_score IS NOT NULL)         AS scored,
                       count(*) FILTER (WHERE llm_score >= :threshold)       AS recommended,
                       round(avg(llm_score) FILTER (WHERE llm_score IS NOT NULL), 1) AS avg_score,
                       max(first_seen)                                       AS newest
                FROM job
                GROUP BY source
                ORDER BY count(*) DESC
                """
            ).bindparams(threshold=threshold)
        )
    )


async def overview(conn: AsyncConnection, threshold: int) -> sa.Row:
    return (
        await conn.execute(
            sa.text(
                """
                SELECT count(*)                                          AS total,
                       count(*) FILTER (WHERE rule_verdict = 'pass')     AS passed_rules,
                       count(*) FILTER (WHERE llm_score IS NOT NULL)     AS scored,
                       count(*) FILTER (WHERE llm_score >= :threshold)   AS recommended,
                       count(*) FILTER (WHERE user_state <> 'new')       AS reviewed,
                       count(*) FILTER (WHERE remote IS TRUE)            AS remote,
                       count(*) FILTER (WHERE salary_min IS NOT NULL)    AS with_salary,
                       count(*) FILTER (WHERE first_seen >= now() - interval '24 hours')
                                                                         AS last_24h,
                       count(DISTINCT company)                           AS companies,
                       min(first_seen)                                   AS first_ever
                FROM job
                """
            ).bindparams(threshold=threshold)
        )
    ).one()


async def country_breakdown(conn: AsyncConnection) -> list[sa.Row]:
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT coalesce(location_country, 'unknown') AS country, count(*) AS n
                FROM job GROUP BY 1 ORDER BY 2 DESC
                """
            )
        )
    )


async def top_companies(conn: AsyncConnection, limit: int = 8) -> list[sa.Row]:
    return list(
        await conn.execute(
            sa.text(
                """
                SELECT company, count(*) AS n
                FROM job
                WHERE company IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT :limit
                """
            ).bindparams(limit=limit)
        )
    )


async def source_health(conn: AsyncConnection) -> list[sa.Row]:
    return list(
        await conn.execute(sa.select(source_state).order_by(source_state.c.source))
    )


async def list_personio_tenants(conn: AsyncConnection) -> list[sa.Row]:
    return list(
        await conn.execute(sa.select(personio_tenant).order_by(personio_tenant.c.slug))
    )


async def enabled_personio_tenants(conn: AsyncConnection) -> list[str]:
    rows = await conn.execute(
        sa.select(personio_tenant.c.slug)
        .where(personio_tenant.c.enabled)
        .order_by(personio_tenant.c.slug)
    )
    return [row.slug for row in rows]


async def add_personio_tenants(conn: AsyncConnection, slugs: list[str]) -> int:
    """Bulk insert, ignoring ones already present. Returns how many rows were actually added."""
    if not slugs:
        return 0
    result = await conn.execute(
        pg_insert(personio_tenant)
        .values([{"slug": slug} for slug in slugs])
        .on_conflict_do_nothing(index_elements=["slug"])
        .returning(personio_tenant.c.slug)
    )
    return len(list(result))


async def set_personio_tenant_enabled(
    conn: AsyncConnection, slug: str, enabled: bool
) -> None:
    await conn.execute(
        sa.update(personio_tenant)
        .where(personio_tenant.c.slug == slug)
        .values(enabled=enabled)
    )


async def delete_personio_tenant(conn: AsyncConnection, slug: str) -> None:
    await conn.execute(sa.delete(personio_tenant).where(personio_tenant.c.slug == slug))


async def ensure_schedule(conn: AsyncConnection) -> None:
    await conn.execute(pg_insert(pipeline_schedule).values(id=1).on_conflict_do_nothing())


async def get_schedule(conn: AsyncConnection) -> sa.Row:
    await ensure_schedule(conn)
    return (
        await conn.execute(sa.select(pipeline_schedule).where(pipeline_schedule.c.id == 1))
    ).one()


async def update_schedule(conn: AsyncConnection, values: dict[str, Any]) -> None:
    await conn.execute(
        pipeline_schedule.update()
        .where(pipeline_schedule.c.id == 1)
        .values(updated_at=sa.func.now(), **values)
    )


async def active_run(conn: AsyncConnection) -> sa.Row | None:
    """A run that is queued or already executing. Used to keep the queue single-file: a manual
    trigger while one is pending is a no-op, and the scheduler does not pile on a second."""
    return (
        await conn.execute(
            sa.select(pipeline_run)
            .where(pipeline_run.c.status.in_(("queued", "running")))
            .order_by(pipeline_run.c.queued_at)
            .limit(1)
        )
    ).one_or_none()


async def latest_run(conn: AsyncConnection) -> sa.Row | None:
    return (
        await conn.execute(
            sa.select(pipeline_run).order_by(pipeline_run.c.queued_at.desc()).limit(1)
        )
    ).one_or_none()


async def recent_runs(conn: AsyncConnection, limit: int = 20) -> list[sa.Row]:
    return list(
        await conn.execute(
            sa.select(pipeline_run).order_by(pipeline_run.c.queued_at.desc()).limit(limit)
        )
    )


async def enqueue_run(
    conn: AsyncConnection, *, trigger: str, lookback_days: int,
    use_llm: bool = True, send_email: bool = True,
) -> int:
    row = (
        await conn.execute(
            pg_insert(pipeline_run)
            .values(
                trigger=trigger, lookback_days=lookback_days,
                use_llm=use_llm, send_email=send_email,
            )
            .returning(pipeline_run.c.id)
        )
    ).one()
    return row.id


async def claim_next_run(conn: AsyncConnection) -> sa.Row | None:
    """Atomically take the oldest due `queued` row and mark it running.

    `FOR UPDATE SKIP LOCKED` on the inner select makes this safe even if two runners ever race;
    the surrounding transaction (connect() opens one) holds the row only until this returns.
    """
    due = (
        sa.select(pipeline_run.c.id)
        .where(pipeline_run.c.status == "queued", pipeline_run.c.not_before <= sa.func.now())
        .order_by(pipeline_run.c.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return (
        await conn.execute(
            pipeline_run.update()
            .where(pipeline_run.c.id == due)
            .values(
                status="running",
                started_at=sa.func.now(),
                attempts=pipeline_run.c.attempts + 1,
            )
            .returning(pipeline_run)
        )
    ).one_or_none()


async def finish_run(
    conn: AsyncConnection, run_id: int, *, status: str,
    report: dict[str, Any] | None, error: str | None,
) -> None:
    await conn.execute(
        pipeline_run.update()
        .where(pipeline_run.c.id == run_id)
        .values(status=status, finished_at=sa.func.now(), report=report, error=error)
    )


async def requeue_run(
    conn: AsyncConnection, run_id: int, *, not_before: datetime, error: str | None,
) -> None:
    """Send a failed run back to the queue for one retry."""
    await conn.execute(
        pipeline_run.update()
        .where(pipeline_run.c.id == run_id)
        .values(status="queued", not_before=not_before, started_at=None, error=error)
    )


async def fail_orphaned_runs(conn: AsyncConnection) -> int:
    """Mark any `running` row as failed. The runner is the sole executor, so a running row at
    its startup means a previous runner died mid-run; leaving it would block the queue forever.
    """
    result = await conn.execute(
        pipeline_run.update()
        .where(pipeline_run.c.status == "running")
        .values(
            status="failed", finished_at=sa.func.now(),
            error="runner restarted while this run was in progress",
        )
    )
    return result.rowcount
