"""Every query in db/queries is executed at least once against a real Postgres.

SQL that compiles is not SQL that runs. This repository has already shipped three statements that
were valid Python, passed every offline test, and failed the moment a server saw them: a `SET LOCAL`
bundled with a SELECT, a dropped f-string prefix that sent a literal `{_HARD_FILTERS}`, and
parameters in positions asyncpg cannot infer a type for. All three were in functions no test had
ever called.

The registry below is explicit rather than introspected, and a completeness check fails when a
query is added without an entry. That is the point: the cost of adding a query is one line here,
and the alternative is discovering the failure in production.

Each call runs in its own transaction and failures are collected, so one broken query reports
itself rather than hiding the other seventy-three.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trouveur.db.engine import connect
from trouveur.db.queries import admin, ingest, jobs, match, users
from trouveur.models import DocumentKind, RawDocument, RunStatus, RunTrigger

MODULES = {"admin": admin, "ingest": ingest, "jobs": jobs, "match": match, "users": users}


def _public_queries() -> set[str]:
    return {
        f"{label}.{name}"
        for label, module in MODULES.items()
        for name, value in vars(module).items()
        if inspect.iscoroutinefunction(value)
        and not name.startswith("_")
        and value.__module__ == module.__name__
    }


def _arguments(ctx: dict) -> dict[str, dict]:
    """Plausible arguments for every query, keyed `module.function`.

    "Plausible" means arguments that make the statement do work: an id that exists, a list that is
    not empty. A query called with an empty list often returns before touching the database, which
    would let a broken statement pass.
    """
    user_id, job_id = ctx["user_id"], ctx["job_id"]
    source, external_id = ctx["source"], ctx["external_id"]
    document = RawDocument(
        source=source, external_id=external_id, kind=DocumentKind.LISTING, payload={"probe": 1}
    )
    return {
        # admin
        "admin.enabled_tenants": {},
        "admin.list_tenants": {"source": source},
        "admin.add_tenants": {"source": source, "scopes": ["probe-tenant"]},
        "admin.set_tenant_enabled": {"source": source, "scope": "probe-tenant", "enabled": False},
        "admin.record_scope_health": {"source": source, "results": ctx["scope_results"]},
        "admin.scope_health": {"failing_only": True},
        "admin.prune_scope_health": {"source": source, "known_scopes": ["probe-tenant"]},
        "admin.remove_tenant": {"source": source, "scope": "probe-tenant"},
        "admin.recent_sweeps": {},
        "admin.source_health": {},
        "admin.corpus_overview": {},
        "admin.derived_coverage": {},
        "admin.description_coverage": {},
        "admin.ensure_schedule": {},
        "admin.get_schedule": {},
        "admin.update_schedule": {"values": {"enabled": True, "run_hour": 6, "run_minute": 30}},
        "admin.enqueue_run": {"trigger": RunTrigger.MANUAL, "only_source": source},
        "admin.claim_next_run": {},
        "admin.finish_run": {"run_id": ctx["run_id"], "status": RunStatus.SUCCESS, "report": {}},
        "admin.request_cancel": {"run_id": ctx["run_id"]},
        "admin.cancel_requested": {"run_id": ctx["run_id"]},
        "admin.start_run_progress": {"run_id": ctx["run_id"], "sources_total": 3},
        "admin.advance_run_progress": {
            "run_id": ctx["run_id"], "done": 1, "current_source": source,
        },
        "admin.running_sweeps": {},
        "admin.typical_sweep_seconds": {},
        "admin.recent_runs": {},
        "admin.active_run": {},
        "admin.fail_orphaned_runs": {},
        "admin.facet_breakdown": {},
        "admin.queue_depth": {},
        # ingest
        "ingest.archive_documents": {"documents": [document]},
        "ingest.latest_documents": {
            "source": source, "external_ids": [external_id], "kind": DocumentKind.LISTING,
        },
        "ingest.stored_content_hashes": {"source": source, "external_ids": [external_id]},
        "ingest.upsert_jobs": {"rows": [ctx["job_row"]]},
        "ingest.ensure_facet_rows": {"job_ids": [job_id]},
        "ingest.touch_seen": {"job_ids": [job_id]},
        "ingest.close_unseen": {
            "source": source, "scopes": ["nothing-matches"],
            "sweep_started_at": datetime.now(UTC) - timedelta(days=3650),
        },
        "ingest.close_stale": {"source": source, "older_than": timedelta(days=3650)},
        "ingest.close_retired": {"job_ids": []},
        "ingest.start_sweep": {"source": source},
        "ingest.record_sweep_progress": {"sweep_id": ctx["sweep_id"], "documents_seen": 7},
        "ingest.finish_sweep": {
            "sweep_id": ctx["sweep_id"], "ok": True, "complete": False, "partitions_total": 1,
            "partitions_done": 1, "partitions_overflowed": 0, "documents_seen": 1,
            "jobs_upserted": 1, "jobs_closed": 0, "error": None,
        },
        # jobs
        "jobs.load_for_derive": {"job_ids": [job_id]},
        "jobs.write_facets": {"rows": [ctx["facet_row"]]},
        "jobs.agency_flags": {"job_ids": [job_id]},
        "jobs.load_for_embedding": {"job_ids": [job_id]},
        "jobs.write_embeddings": {"rows": [(job_id, "probe:probe:384", [0.01] * 384)]},
        "jobs.load_for_dedup": {"job_ids": [job_id]},
        "jobs.write_dedup_markers": {"rows": [(job_id, b"probe-group")], "dedup_version": 1},
        "jobs.detail_targets": {"job_ids": [job_id]},
        # match
        "match.dense_candidates": {
            "profile": ctx["profile"], "vector": [0.01] * 384, "limit": 5,
        },
        "match.lexical_candidates": {"profile": ctx["profile"], "query": "ingenieur", "limit": 5},
        "match.upsert_matches": {"rows": [ctx["match_row"]]},
        "match.apply_rule_verdicts": {
            "rows": [{
                "user_id": user_id, "job_id": job_id, "rule_verdict": "pass", "rule_reason": "ok",
            }]
        },
        "match.pending_rerank": {"user_id": user_id, "profile_version": 1, "limit": 5},
        "match.count_pending_rerank": {"user_id": user_id, "profile_version": 1},
        "match.scoreable_rows": {"job_ids": [job_id]},
        "match.pending_rules": {"user_id": user_id, "limit": 5},
        "match.cached_scores": {
            "user_id": user_id, "profile_version": 1, "hashes": [ctx["content_hash"]],
        },
        "match.put_cached_scores": {"rows": [ctx["cache_row"]]},
        "match.apply_scores": {"user_id": user_id, "rows": [ctx["score_row"]]},
        "match.recommendations": {"user_id": user_id},
        "match.search_jobs": {"user_id": user_id, "query": "ingenieur", "country": "DE"},
        "match.get_job": {"user_id": user_id, "public_id": ctx["public_id"]},
        "match.set_state": {"user_id": user_id, "job_id": job_id, "state": "saved"},
        "match.pending_digest": {"user_id": user_id},
        "match.mark_notified": {"user_id": user_id, "job_ids": [job_id]},
        "match.get_query_expansion": {
            "user_id": user_id, "profile_version": 1, "expansion_version": 1,
        },
        "match.put_query_expansion": {
            "user_id": user_id, "profile_version": 1, "expansion_version": 1,
            "queries": ["ingenieur"],
        },
        "match.match_stats": {"user_id": user_id},
        # users
        "users.get_user_by_username": {"username": ctx["username"]},
        "users.get_user": {"user_id": user_id},
        "users.list_users": {},
        "users.create_user": {"username": "probe-user", "password_hash": "argon2$probe"},
        "users.record_login_result": {
            "user_id": user_id, "success": False, "lockout_minutes": 15, "max_attempts": 5,
        },
        "users.get_profile": {"user_id": user_id},
        "users.save_profile": {"user_id": user_id, "values": {"title": "Probe"}},
        "users.get_credential": {"user_id": user_id},
        "users.save_credential": {
            "user_id": user_id, "api_key_encrypted": b"probe", "api_key_fingerprint": "probe",
            "model": "probe/model", "provider_pin": None, "monthly_budget_usd": Decimal("1"),
        },
        "users.update_budget": {"user_id": user_id, "monthly_budget_usd": Decimal("2")},
        "users.month_spend": {"user_id": user_id},
        "users.add_spend": {
            "user_id": user_id, "tokens_in": 1, "tokens_out": 1, "cost_usd": Decimal("0.001"),
        },
        "users.spend_history": {"user_id": user_id},
        "users.delete_credential": {"user_id": user_id},
    }


@pytest.fixture
async def context(seeded):
    """Ids and row shapes that make every query touch something real."""
    from trouveur.sources.base import ScopeResult

    async with connect() as conn:
        row = (
            await conn.exec_driver_sql(
                "SELECT id, source, external_id, public_id, content_hash FROM job LIMIT 1"
            )
        ).one()
        run_id = await admin.enqueue_run(conn, trigger=RunTrigger.MANUAL)
        sweep_id, _ = await ingest.start_sweep(conn, row.source)
        user = (await conn.exec_driver_sql("SELECT username FROM app_user LIMIT 1")).scalar()

    return {
        "user_id": seeded["user_id"],
        "profile": seeded["profile"],
        "username": user,
        "job_id": row.id,
        "source": row.source,
        "external_id": row.external_id,
        "public_id": str(row.public_id),
        "content_hash": row.content_hash,
        "run_id": run_id,
        "sweep_id": sweep_id,
        "scope_results": [ScopeResult(scope="probe-tenant", ok=True, documents=1)],
        "job_row": {
            "source": row.source, "external_id": row.external_id, "scope": None,
            "url": "https://example.test/probe", "title": "Probe", "company": "Probe GmbH",
            "description": None, "posted_at": None, "updated_at": None, "closes_at": None,
            "locations": [], "location_text": "", "salary_amount_min": None,
            "salary_amount_max": None, "salary_currency": None, "salary_period": "UNKNOWN",
            "remote_hint": None, "employment_type_hint": None, "agency_hint": None,
            "language_hint": None, "department_hint": None, "content_hash": row.content_hash,
            "normalize_version": 1, "listing_document_id": None, "detail_document_id": None,
        },
        "facet_row": {
            "job_id": row.id, "derive_version": 1, "countries": ["DE"], "regions": [],
            "cities": ["Berlin"], "work_mode": "remote", "seniority": "senior",
            "employment_type": "full_time", "salary_min_eur_year": None,
            "salary_max_eur_year": None, "salary_annualised": False, "language": "de",
            "is_agency": None, "skills": ["python"], "derived_at": None,
        },
        "match_row": {
            "user_id": seeded["user_id"], "job_id": row.id, "profile_version": 1,
            "retrieval_score": 0.5, "dense_rank": 1, "lexical_rank": 1,
        },
        "cache_row": {
            "content_hash": row.content_hash, "user_id": seeded["user_id"],
            "profile_version": 1, "score": 80, "reason": "probe", "red_flags": [],
            "model": "probe/model",
        },
        "score_row": {
            "job_id": row.id, "score": 80, "reason": "probe", "red_flags": ["probe"],
            "profile_version": 1,
        },
    }


async def test_every_query_has_an_entry_in_the_registry(context):
    """Adding a query without executing it must fail here, not in production.

    Without this the registry silently stops covering the module it is supposed to cover, and the
    suite keeps reporting green over a shrinking fraction of the SQL.
    """
    missing = _public_queries() - set(_arguments(context))
    assert not missing, (
        f"queries with no execution entry: {sorted(missing)}. "
        "Add arguments to _arguments() so the statement is executed at least once."
    )


async def test_registry_names_only_queries_that_exist(context):
    stale = set(_arguments(context)) - _public_queries()
    assert not stale, f"registry entries for queries that no longer exist: {sorted(stale)}"


async def test_every_query_executes(context):
    """Call all of them. Failures are collected so one break does not mask the rest."""
    arguments = _arguments(context)
    failures = []
    for name in sorted(arguments):
        module_name, function_name = name.split(".")
        function = getattr(MODULES[module_name], function_name)
        try:
            # One transaction each: a statement that fails must not abort the ones after it.
            async with connect() as conn:
                await function(conn, **arguments[name])
        except Exception as exc:  # noqa: BLE001 - reporting every breakage is the whole point
            failures.append(f"{name}: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")

    assert not failures, "queries that do not execute:\n" + "\n".join(failures)
