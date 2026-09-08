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
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trouveur.config import get_settings
from trouveur.crypto import encrypt, fingerprint
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.match.pipeline import profile_from_row
from trouveur.models import UserState
from trouveur.sources.registry import NORMALIZERS
from trouveur.web import auth

log = logging.getLogger(__name__)

BASE = Path(__file__).parent
app = FastAPI(title="Trouveur", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _commas(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _decimal(raw: str, default: Decimal) -> Decimal:
    try:
        return Decimal(raw.strip() or "0")
    except (InvalidOperation, ValueError):
        return default


def _session(request: Request) -> dict | None:
    return auth.read_session(get_settings(), request)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse("/recommendations" if _session(request) else "/login", 303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if _session(request):
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
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        profile_row = await users_q.get_profile(conn, session["uid"])
        threshold = profile_row.notify_threshold if profile_row else 70
        jobs = await match_q.recommendations(conn, session["uid"], threshold)
        stats = await match_q.match_stats(conn, session["uid"])
        credential = await users_q.get_credential(conn, session["uid"])
    return templates.TemplateResponse(
        request,
        "recommendations.html",
        {
            "active": "recommendations",
            "jobs": jobs,
            "threshold": threshold,
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
    session = _session(request)
    if not session:
        return _login_redirect()
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
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        job = await match_q.get_job(conn, session["uid"], public_id)
    if job is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request, "job.html", {"active": "search", "job": job, "username": session["u"]}
    )


@app.post("/job/{job_id}/state", response_class=HTMLResponse)
async def set_state(request: Request, job_id: int, state: str = Form(...)):
    session = _session(request)
    if not session:
        return _login_redirect()
    try:
        parsed = UserState(state)
    except ValueError:
        return HTMLResponse("Unknown state", status_code=400)
    async with connect() as conn:
        await match_q.set_state(conn, session["uid"], job_id, parsed.value)
    return HTMLResponse(f'<span class="state state-{parsed.value}">{parsed.value}</span>')


@app.get("/profile", response_class=HTMLResponse)
async def profile_form(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
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
    notify_threshold: int = Form(70),
):
    session = _session(request)
    if not session:
        return _login_redirect()
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
        "notify_threshold": min(max(notify_threshold, 0), 100),
    }
    async with connect() as conn:
        version, rescore = await users_q.save_profile(conn, session["uid"], values)
    log.info(
        "profile saved for user %s (version %d, rescore=%s)", session["uid"], version, rescore
    )
    return RedirectResponse(f"/profile?saved=1&rescore={int(rescore)}", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
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
    session = _session(request)
    if not session:
        return _login_redirect()
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
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        await users_q.delete_credential(conn, session["uid"])
    return RedirectResponse("/settings?deleted=1", status_code=303)


@app.get("/companies", response_class=HTMLResponse)
async def companies(request: Request, added: int = 0, rejected: str = ""):
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        boards = await admin_q.list_greenhouse_boards(conn)
    return templates.TemplateResponse(
        request,
        "companies.html",
        {
            "active": "companies",
            "boards": boards,
            "added": added,
            "rejected": rejected,
            "username": session["u"],
        },
    )


@app.post("/companies")
async def companies_add(request: Request, slugs: str = Form("")):
    session = _session(request)
    if not session:
        return _login_redirect()
    candidates = [
        slug.lower() for slug in _lines(slugs.replace(",", "\n")) if slug.replace("-", "").isalnum()
    ]
    rejected = [slug for slug in _lines(slugs.replace(",", "\n")) if slug.lower() not in candidates]
    async with connect() as conn:
        added = await admin_q.add_greenhouse_boards(conn, candidates)
    return RedirectResponse(
        f"/companies?added={added}&rejected={','.join(rejected)}", status_code=303
    )


@app.post("/companies/{slug}/toggle")
async def companies_toggle(request: Request, slug: str, enabled: bool = Form(False)):
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        await admin_q.set_greenhouse_board_enabled(conn, slug, enabled)
    return RedirectResponse("/companies", status_code=303)


@app.post("/companies/{slug}/delete")
async def companies_delete(request: Request, slug: str):
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        await admin_q.delete_greenhouse_board(conn, slug)
    return RedirectResponse("/companies", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        overview = await admin_q.corpus_overview(conn)
        coverage = await admin_q.derived_coverage(conn)
        health = await admin_q.source_health(conn)
        queues = await admin_q.queue_depth(conn)
        countries = await admin_q.facet_breakdown(conn)
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
            "stats": stats,
            "spend": spend,
            "sources": sorted(NORMALIZERS),
            "username": session["u"],
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        schedule = await admin_q.get_schedule(conn)
        runs = await admin_q.recent_runs(conn)
        sweeps = await admin_q.recent_sweeps(conn)
        active = await admin_q.active_run(conn)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "active": "admin",
            "schedule": schedule,
            "runs": runs,
            "sweeps": sweeps,
            "active_run": active,
            "sources": sorted(NORMALIZERS),
            "username": session["u"],
        },
    )


@app.post("/admin/schedule")
async def admin_schedule(
    request: Request,
    enabled: bool = Form(False),
    run_hour: int = Form(7),
    run_minute: int = Form(0),
):
    session = _session(request)
    if not session:
        return _login_redirect()
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
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        await admin_q.enqueue_run(
            conn, trigger="manual", only_source=only_source or None, backfill=backfill
        )
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/runs", response_class=HTMLResponse)
async def admin_runs_fragment(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    async with connect() as conn:
        runs = await admin_q.recent_runs(conn)
        active = await admin_q.active_run(conn)
    return templates.TemplateResponse(
        request, "_admin_runs.html", {"runs": runs, "active_run": active}
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
