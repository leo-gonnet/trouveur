"""Routes, auth and templates.

The web layer reads the database and enqueues work. It never fetches from a source, never runs a
sweep and never calls a model directly: web and runner share nothing but Postgres, so either can
be down without taking the other with it.

It also never re-derives. Everything shown here comes from stored facets; a template that parses
a location or infers a work mode would be a second implementation of a question already answered
in trouveur/ingest/derive.py, and the two would drift.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined

from trouveur.config import get_settings
from trouveur.crypto import encrypt, fingerprint
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.match.pipeline import profile_from_row
from trouveur.models import RunTrigger, UserState
from trouveur.sources.registry import NORMALIZERS
from trouveur.web import auth
from trouveur.work import backlog

log = logging.getLogger(__name__)

BASE = Path(__file__).parent
app = FastAPI(title="Trouveur", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
# StrictUndefined, not Jinja's default. The default renders an unknown attribute as an empty
# string, so a template reading a field its query does not select produces a blank cell and a
# green test suite -- the exact silent-wrong-answer failure this codebase is built to avoid.
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.undefined = StrictUndefined


def _lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _commas(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _decimal(raw: str, default: Decimal) -> Decimal:
    try:
        return Decimal(raw.strip() or "0")
    except (InvalidOperation, ValueError):
        return default


# Public by opt-in, never by omission. A route absent from this set requires a session, so the
# failure mode of forgetting to think about auth is "locked", not "open".
PUBLIC_PATHS = frozenset({"/", "/login", "/logout", "/healthz"})
PUBLIC_PREFIXES = ("/static/",)


def _session(request: Request) -> dict | None:
    return auth.read_session(get_settings(), request)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Enforce authentication before anything else looks at the request.

    Middleware rather than a per-handler check or a dependency, because both of those run after
    FastAPI has already parsed and validated the request body: an anonymous POST with a malformed
    form was answering 422, which means attacker-controlled input was being processed before the
    caller was known. Nothing leaked, but the ordering was safe only by accident.

    Doing it here also removes the two-line check that was repeated in every handler, which is the
    repetition that guarantees somebody eventually forgets it on a new route.
    """
    session = _session(request)
    request.state.session = session
    if session is None and not _is_public(request.url.path):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse("/recommendations" if request.state.session else "/login", 303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if request.state.session:
        return RedirectResponse("/recommendations", 303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    settings = get_settings()
    async with connect() as conn:
        user_id, message = await auth.authenticate(conn, settings, username, password)
    if user_id is None:
        return templates.TemplateResponse(
            request, "login.html", {"error": message}, status_code=401
        )
    response = RedirectResponse("/recommendations", status_code=303)
    auth.set_cookie(response, settings, auth.issue_session(settings, user_id, username))
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_cookie(response)
    return response


@app.get("/recommendations", response_class=HTMLResponse)
async def recommendations(request: Request):
    session = request.state.session
    async with connect() as conn:
        profile_row = await users_q.get_profile(conn, session["uid"])
        # Everything that was reranked is shown, so the page is exactly as long as the user's
        # rerank budget -- the one number they already set, rather than a second one to tune.
        limit = profile_row.rerank_limit if profile_row else 150
        jobs = await match_q.recommendations(conn, session["uid"], limit)
        stats = await match_q.match_stats(conn, session["uid"])
        credential = await users_q.get_credential(conn, session["uid"])
    return templates.TemplateResponse(
        request,
        "recommendations.html",
        {
            "active": "recommendations",
            "jobs": jobs,
            "stats": stats,
            "has_key": credential is not None,
            "username": session["u"],
        },
    )


@app.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = "",
    country: str = "",
    work_mode: str = "",
    include_closed: bool = False,
    page: int = 1,
):
    session = request.state.session
    page = max(page, 1)
    limit = 50
    async with connect() as conn:
        jobs = await match_q.search_jobs(
            conn,
            session["uid"],
            query=q,
            country=country,
            work_mode=work_mode,
            include_closed=include_closed,
            limit=limit,
            offset=(page - 1) * limit,
        )
    context = {
        "active": "search",
        "jobs": jobs,
        "q": q,
        "country": country,
        "work_mode": work_mode,
        "include_closed": include_closed,
        "page": page,
        "has_more": len(jobs) == limit,
        "username": session["u"],
    }
    # HTMX swaps only the results table; a full navigation renders the whole page.
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_results.html", context)
    return templates.TemplateResponse(request, "search.html", context)


@app.get("/job/{public_id}", response_class=HTMLResponse)
async def job_detail(request: Request, public_id: str):
    session = request.state.session
    async with connect() as conn:
        job = await match_q.get_job(conn, session["uid"], public_id)
    if job is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request, "job.html", {"active": "search", "job": job, "username": session["u"]}
    )


@app.post("/job/{job_id}/state", response_class=HTMLResponse)
async def set_state(request: Request, job_id: int, state: str = Form(...)):
    session = request.state.session
    try:
        parsed = UserState(state)
    except ValueError:
        return HTMLResponse("Unknown state", status_code=400)
    async with connect() as conn:
        await match_q.set_state(conn, session["uid"], job_id, parsed.value)
    return HTMLResponse(f'<span class="state state-{parsed.value}">{parsed.value}</span>')


@app.get("/profile", response_class=HTMLResponse)
async def profile_form(request: Request):
    session = request.state.session
    async with connect() as conn:
        row = await users_q.get_profile(conn, session["uid"])
        profile = profile_from_row(row)
        # Priced before the edit, not billed after it: changing a scoring field invalidates this
        # user's cached scores, and they should see what that costs before committing to it.
        pending = await match_q.count_pending_rerank(conn, session["uid"], profile.version + 1)
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "active": "profile",
            "profile": profile,
            "rescore_estimate": pending,
            "scoring_fields": sorted(users_q.SCORING_FIELDS),
            "username": session["u"],
        },
    )


@app.post("/profile", response_class=HTMLResponse)
async def profile_save(
    request: Request,
    title: str = Form(""),
    years_experience: int = Form(0),
    objectives: str = Form(""),
    languages: str = Form(""),
    must_have: str = Form(""),
    deal_breakers: str = Form(""),
    keywords: str = Form(""),
    countries: str = Form(""),
    cities: str = Form(""),
    work_modes: str = Form(""),
    seniorities: str = Form(""),
    employment_types: str = Form(""),
    min_salary_eur_year: str = Form("0"),
    retrieval_limit: int = Form(400),
    rerank_limit: int = Form(150),
):
    session = request.state.session
    values = {
        "title": title.strip(),
        "years_experience": max(0, years_experience),
        "objectives": objectives.strip(),
        "languages": _commas(languages),
        "must_have": _lines(must_have),
        "deal_breakers": _lines(deal_breakers),
        "keywords": _lines(keywords),
        "countries": [item.upper() for item in _commas(countries)],
        "cities": _commas(cities),
        "work_modes": _commas(work_modes),
        "seniorities": _commas(seniorities),
        "employment_types": _commas(employment_types),
        "min_salary_eur_year": _decimal(min_salary_eur_year, Decimal(0)),
        "retrieval_limit": min(max(retrieval_limit, 25), 2000),
        "rerank_limit": min(max(rerank_limit, 0), 1000),
    }
    async with connect() as conn:
        version, rescore = await users_q.save_profile(conn, session["uid"], values)
    log.info(
        "profile saved for user %s (version %d, rescore=%s)", session["uid"], version, rescore
    )
    return RedirectResponse(f"/profile?saved=1&rescore={int(rescore)}", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request):
    session = request.state.session
    async with connect() as conn:
        credential = await users_q.get_credential(conn, session["uid"])
        spend = await users_q.month_spend(conn, session["uid"])
        history = await users_q.spend_history(conn, session["uid"])
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "default_model": settings.default_llm_model,
            "default_provider": settings.default_llm_provider or "",
            # Never the key itself. The form shows a fingerprint so the user can tell which key is
            # stored without this page being able to disclose it to anyone who reaches it.
            "credential": credential,
            "spend": spend,
            "history": history,
            "username": session["u"],
        },
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(
    request: Request,
    api_key: str = Form(""),
    model: str = Form(""),
    provider_pin: str = Form(""),
    monthly_budget_usd: str = Form("5"),
):
    session = request.state.session
    async with connect() as conn:
        budget = _decimal(monthly_budget_usd, Decimal(5))
        chosen_model = model.strip() or get_settings().default_llm_model
        key = api_key.strip()
        if key:
            await users_q.save_credential(
                conn,
                session["uid"],
                api_key_encrypted=encrypt(key),
                api_key_fingerprint=fingerprint(key),
                model=chosen_model,
                provider_pin=provider_pin.strip() or None,
                monthly_budget_usd=budget,
            )
        else:
            # An empty key field means "leave the stored key alone", not "delete it": re-typing a
            # secret to change an unrelated budget is how secrets end up in shell history.
            existing = await users_q.get_credential(conn, session["uid"])
            if existing is not None:
                await users_q.update_budget(conn, session["uid"], budget)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/delete-key")
async def settings_delete_key(request: Request):
    session = request.state.session
    async with connect() as conn:
        await users_q.delete_credential(conn, session["uid"])
    return RedirectResponse("/settings?deleted=1", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    session = request.state.session
    async with connect() as conn:
        overview = await admin_q.corpus_overview(conn)
        coverage = await admin_q.derived_coverage(conn)
        health = await admin_q.source_health(conn)
        queues = await admin_q.queue_depth(conn)
        countries = await admin_q.facet_breakdown(conn)
        scopes = await admin_q.list_tenants(conn)
        stats = await match_q.match_stats(conn, session["uid"])
        spend = await users_q.month_spend(conn, session["uid"])
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "overview": overview,
            "coverage": coverage,
            "health": health,
            "queues": queues,
            "countries": countries,
            "scopes": scopes,
            "stats": stats,
            "spend": spend,
            "sources": sorted(NORMALIZERS),
            "username": session["u"],
        },
    )


async def _run_status() -> dict:
    """Everything the live panel shows, from rows the runner writes as it goes.

    The estimate is the sum of each remaining source's median duration over its own last few
    sweeps. Sources differ by two orders of magnitude -- a board is seconds, Arbeitsagentur is
    half an hour -- so an average across sources would be fiction. A source with no history
    contributes nothing and makes the estimate partial, which the page says outright rather than
    filling the gap with a guess.
    """
    async with connect() as conn:
        active = await admin_q.active_run(conn)
        running = await admin_q.running_sweeps(conn)
        queues = await backlog(conn)
        typical = await admin_q.typical_sweep_seconds(conn)
        done_sources = set()
        remaining_estimate = None
        unknown_sources = 0
        if active is not None and active.sources_total:
            done_sources = {row.source for row in await admin_q.recent_sweeps(conn, limit=40)}
            pending = max(active.sources_total - (active.sources_done or 0), 0)
            if pending:
                known = [typical[name] for name in typical if name not in done_sources]
                unknown_sources = max(pending - len(known), 0)
                remaining_estimate = sum(sorted(known, reverse=True)[:pending]) or None
    now = datetime.now(UTC)
    return {
        "active_run": active,
        "running_sweeps": running,
        "queues": queues,
        "eta_seconds": remaining_estimate,
        "eta_partial": bool(unknown_sources),
        # Elapsed times are computed here rather than in the template, which has no clock and
        # should not grow one.
        "elapsed_seconds": (
            (now - active.started_at).total_seconds()
            if active is not None and active.started_at
            else 0
        ),
        "sweep_elapsed": {
            row.source: (now - row.started_at).total_seconds() for row in running
        },
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    session = request.state.session
    async with connect() as conn:
        schedule = await admin_q.get_schedule(conn)
        runs = await admin_q.recent_runs(conn)
        sweeps = await admin_q.recent_sweeps(conn)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "active": "admin",
            "schedule": schedule,
            "runs": runs,
            "sweeps": sweeps,
            "sources": sorted(NORMALIZERS),
            "username": session["u"],
            **await _run_status(),
        },
    )


@app.post("/admin/schedule")
async def admin_schedule(
    request: Request,
    enabled: bool = Form(False),
    run_hour: int = Form(7),
    run_minute: int = Form(0),
):
    async with connect() as conn:
        await admin_q.update_schedule(
            conn,
            {
                "enabled": enabled,
                "run_hour": min(max(run_hour, 0), 23),
                "run_minute": min(max(run_minute, 0), 59),
            },
        )
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/run")
async def admin_run(request: Request, only_source: str = Form(""), backfill: bool = Form(False)):
    """Enqueue a run. The web app never executes one; the runner owns that entirely."""
    async with connect() as conn:
        await admin_q.enqueue_run(
            conn,
            trigger=RunTrigger.MANUAL,
            only_source=only_source or None,
            backfill=backfill,
        )
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/run/{run_id}/cancel")
async def admin_cancel_run(request: Request, run_id: int):
    """Ask a run to stop. A queued one ends at once; a running one stops between sources."""
    async with connect() as conn:
        await admin_q.request_cancel(conn, run_id)
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/status", response_class=HTMLResponse)
async def admin_status_fragment(request: Request):
    return templates.TemplateResponse(request, "_admin_status.html", await _run_status())


@app.get("/admin/runs", response_class=HTMLResponse)
async def admin_runs_fragment(request: Request):
    async with connect() as conn:
        runs = await admin_q.recent_runs(conn)
        active = await admin_q.active_run(conn)
    return templates.TemplateResponse(
        request, "_admin_runs.html", {"runs": runs, "active_run": active}
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
