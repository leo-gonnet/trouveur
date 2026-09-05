"""Web UI: login, dashboard, recommendations, search, profile, sources, companies, admin.

Binds loopback only; Caddy terminates TLS in front of it. Reads the database and never fetches
from sources or runs the pipeline: the admin page only enqueues `pipeline_run` rows for the
runner service to execute (see AGENTS.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trouveur.config import get_settings
from trouveur.db import queries as q
from trouveur.db.engine import connect
from trouveur.models import ProfileData, UserState
from trouveur.sources import AVAILABLE_SOURCES, SOURCE_NAMES
from trouveur.sources.personio import parse_tenants
from trouveur.web import auth

_HERE = Path(__file__).parent

app = FastAPI(title="Trouveur", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(_HERE / "templates"))


def _split_lines(raw: str) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _split_commas(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


async def _current_user(request: Request) -> str | None:
    return auth.read_session(get_settings(), request)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if await _current_user(request) is None:
        return _login_redirect()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    settings = get_settings()
    async with connect() as conn:
        ok, message = await auth.authenticate(conn, settings, username, password)
    if not ok:
        return templates.TemplateResponse(
            request, "login.html", {"error": message}, status_code=401
        )
    response = RedirectResponse("/dashboard", status_code=303)
    auth.set_cookie(response, settings, auth.issue_session(settings, username))
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_cookie(response)
    return response


DASHBOARD_DAYS = 21


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if await _current_user(request) is None:
        return _login_redirect()
    async with connect() as conn:
        await q.ensure_profile(conn)
        profile_row = await q.get_profile(conn)
        threshold = profile_row.notify_threshold if profile_row else 70

        overview = await q.overview(conn, threshold)
        sources = await q.source_quality(conn, threshold)
        health = {row.source: row for row in await q.source_health(conn)}
        enabled_sources = set(await q.enabled_sources(conn))
        daily = await q.daily_intake(conn, DASHBOARD_DAYS)
        countries = await q.country_breakdown(conn)
        companies = await q.top_companies(conn, limit=8)

    chart = _build_chart(daily, DASHBOARD_DAYS)
    best_sources = _rank_by_match_rate(sources)
    return templates.TemplateResponse(
        request, "dashboard.html", {
            "overview": overview, "sources": sources, "health": health,
            "chart": chart, "countries": countries, "companies": companies,
            "best_sources": best_sources, "threshold": threshold,
            "enabled_sources": enabled_sources,
        },
    )


def _rank_by_match_rate(sources) -> list[dict]:
    """Rank sources by recommended/scraped -- the number that says whether a source is worth
    its request budget, as opposed to raw volume (which `sources` is already sorted by)."""
    ranked = [
        {
            "source": s.source, "scraped": s.scraped, "recommended": s.recommended,
            "rate": (s.recommended / s.scraped) if s.scraped else 0.0,
        }
        for s in sources
        if s.scraped >= 5  # too few samples to call a rate meaningful
    ]
    ranked.sort(key=lambda r: r["rate"], reverse=True)
    return ranked


_PALETTE = ["#2f6fed", "#17855c", "#b45309", "#8957e5", "#c2352c", "#0891b2", "#be185d"]


def _build_chart(daily_rows, days: int) -> dict:
    """Reshape daily_intake() rows into a day x source matrix a template can render as bars."""
    today = datetime.now(UTC).date()
    order = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    sources = sorted({row.source for row in daily_rows})
    by_day: dict = {day: dict.fromkeys(sources, 0) for day in order}
    for row in daily_rows:
        if row.day in by_day:
            by_day[row.day][row.source] = row.n

    max_total = max((sum(counts.values()) for counts in by_day.values()), default=0)
    columns = [
        {"day": day, "label": day.strftime("%d.%m"), "counts": by_day[day],
         "total": sum(by_day[day].values())}
        for day in order
    ]
    colors = dict(zip(sources, _PALETTE * (len(sources) // len(_PALETTE) + 1), strict=False))
    return {
        "sources": sources, "columns": columns, "max_total": max_total or 1,
        "colors": colors,
    }


@app.get("/recommendations", response_class=HTMLResponse)
async def recommendations(request: Request):
    if await _current_user(request) is None:
        return _login_redirect()
    async with connect() as conn:
        await q.ensure_profile(conn)
        profile_row = await q.get_profile(conn)
        threshold = profile_row.notify_threshold if profile_row else 70
        rows = await q.recommendations(conn, threshold=threshold, limit=50)
        overview = await q.overview(conn, threshold)
    return templates.TemplateResponse(
        request, "recommendations.html",
        {"jobs": rows, "threshold": threshold, "overview": overview},
    )


@app.post("/jobs/{job_id}/state", response_class=HTMLResponse)
async def set_state(request: Request, job_id: int, state: str = Form(...)):
    if await _current_user(request) is None:
        return Response(status_code=401)
    try:
        new_state = UserState(state)
    except ValueError:
        return Response("unknown state", status_code=400)
    async with connect() as conn:
        await q.set_user_state(conn, job_id, new_state)
    # HTMX swaps this fragment in place of the row's action buttons.
    _tone = {"interested": "ok", "applied": "ok", "rejected": "bad"}.get(new_state.value, "")
    return HTMLResponse(f'<span class="pill {_tone}">marked {new_state.value}</span>')


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q_: str = "", country: str = "", remote: str = ""):
    if await _current_user(request) is None:
        return _login_redirect()
    query = request.query_params.get("q", q_)
    remote_filter = {"yes": True, "no": False}.get(remote)
    async with connect() as conn:
        rows = await q.search_jobs(
            conn, query, country=country or None, remote=remote_filter, limit=100
        )
    context = {"jobs": rows, "query": query, "country": country, "remote": remote}
    # HTMX active-search asks for just the results table.
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_results.html", context)
    return templates.TemplateResponse(request, "search.html", context)


@app.get("/profile", response_class=HTMLResponse)
async def profile_form(request: Request):
    if await _current_user(request) is None:
        return _login_redirect()
    async with connect() as conn:
        await q.ensure_profile(conn)
        row = await q.get_profile(conn)
    data = ProfileData.model_validate(
        {k: v for k, v in row._mapping.items() if k != "id"}
    ) if row else ProfileData()
    return templates.TemplateResponse(request, "profile.html", {"p": data, "saved": False})


@app.post("/profile", response_class=HTMLResponse)
async def profile_save(
    request: Request,
    title: str = Form(""),
    years_experience: int = Form(0),
    languages: str = Form(""),
    objectives: str = Form(""),
    must_have: str = Form(""),
    deal_breakers: str = Form(""),
    keywords: str = Form(""),
    cities: str = Form(""),
    countries: str = Form(""),
    remote_only: bool = Form(False),
    min_salary_eur_year: str = Form("0"),
    notify_threshold: int = Form(70),
):
    if await _current_user(request) is None:
        return _login_redirect()

    values = {
        "title": title.strip(),
        "years_experience": max(0, years_experience),
        "languages": _split_commas(languages),
        "objectives": objectives.strip(),
        "must_have": _split_lines(must_have),
        "deal_breakers": _split_lines(deal_breakers),
        "keywords": _split_commas(keywords),
        "cities": _split_commas(cities),
        "countries": [c.upper() for c in _split_commas(countries)] or ["AT", "DE", "CH"],
        "remote_only": bool(remote_only),
        "min_salary_eur_year": Decimal(min_salary_eur_year or 0),
        "notify_threshold": max(0, min(100, notify_threshold)),
    }
    # save_profile also bumps the profile version, which invalidates cached LLM scores: they were
    # judged against the old objectives and are no longer valid answers.
    async with connect() as conn:
        await q.save_profile(conn, values)
        row = await q.get_profile(conn)

    data = ProfileData.model_validate({k: v for k, v in row._mapping.items() if k != "id"})
    return templates.TemplateResponse(request, "profile.html", {"p": data, "saved": True})


RECENT_RUNS = 15


async def _admin_context(request: Request) -> dict:
    async with connect() as conn:
        schedule = await q.get_schedule(conn)
        runs = await q.recent_runs(conn, limit=RECENT_RUNS)
        active = await q.active_run(conn)
    return {"schedule": schedule, "runs": runs, "active": active}


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    if await _current_user(request) is None:
        return _login_redirect()
    return templates.TemplateResponse(request, "admin.html", await _admin_context(request))


@app.get("/admin/runs", response_class=HTMLResponse)
async def admin_runs(request: Request):
    """HTMX polls this fragment while a run is active."""
    if await _current_user(request) is None:
        return Response(status_code=401)
    return templates.TemplateResponse(request, "_admin_runs.html", await _admin_context(request))


@app.post("/admin/schedule", response_class=HTMLResponse)
async def admin_schedule(
    request: Request,
    enabled: bool = Form(False),
    run_hour: int = Form(7),
    run_minute: int = Form(0),
    lookback_days: int = Form(1),
):
    if await _current_user(request) is None:
        return _login_redirect()
    values = {
        "enabled": bool(enabled),
        "run_hour": max(0, min(23, run_hour)),
        "run_minute": max(0, min(59, run_minute)),
        "lookback_days": max(1, min(90, lookback_days)),
    }
    async with connect() as conn:
        await q.update_schedule(conn, values)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/run", response_class=HTMLResponse)
async def admin_run(
    request: Request,
    days: int = Form(1),
    use_llm: bool = Form(False),
    send_email: bool = Form(False),
):
    if await _current_user(request) is None:
        return _login_redirect()
    # Enqueue only; the runner service picks it up. A run already pending makes this a no-op.
    async with connect() as conn:
        if await q.active_run(conn) is None:
            await q.enqueue_run(
                conn, trigger="manual", lookback_days=max(1, min(90, days)),
                use_llm=bool(use_llm), send_email=bool(send_email),
            )
    return RedirectResponse("/admin", status_code=303)


@app.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    if await _current_user(request) is None:
        return _login_redirect()
    async with connect() as conn:
        activation = await q.source_activation_map(conn)
        tenant_count = len(await q.enabled_personio_tenants(conn))
    rows = [
        {"info": info, "enabled": activation.get(info.name, False)}
        for info in AVAILABLE_SOURCES
    ]
    return templates.TemplateResponse(
        request, "sources.html",
        {"sources": rows, "active_count": sum(r["enabled"] for r in rows),
         "tenant_count": tenant_count},
    )


@app.post("/sources/{name}/toggle", response_class=HTMLResponse)
async def sources_toggle(request: Request, name: str, enabled: bool = Form(False)):
    if await _current_user(request) is None:
        return _login_redirect()
    # Only names from the registry, so a stale form or a typed URL cannot create a row for a
    # source that will never run.
    if name not in SOURCE_NAMES:
        return Response("unknown source", status_code=404)
    async with connect() as conn:
        await q.set_source_enabled(conn, name, bool(enabled))
    return RedirectResponse("/sources", status_code=303)


@app.get("/companies", response_class=HTMLResponse)
async def companies_page(request: Request, added: int = 0, rejected: str = ""):
    if await _current_user(request) is None:
        return _login_redirect()
    async with connect() as conn:
        tenants = await q.list_personio_tenants(conn)
        personio_active = "personio" in await q.enabled_sources(conn)
    return templates.TemplateResponse(
        request,
        "companies.html",
        {
            "tenants": tenants,
            "added": added,
            "rejected": _split_lines(rejected),
            "personio_active": personio_active,
        },
    )


@app.post("/companies", response_class=HTMLResponse)
async def companies_add(request: Request, tenants: str = Form("")):
    if await _current_user(request) is None:
        return _login_redirect()
    # Anything unrecognised is reported back rather than dropped, so a bad paste is visible.
    slugs, rejected = parse_tenants(tenants)
    async with connect() as conn:
        added = await q.add_personio_tenants(conn, slugs)
    query = f"?added={added}"
    if rejected:
        query += "&rejected=" + quote("\n".join(rejected))
    return RedirectResponse(f"/companies{query}", status_code=303)


@app.post("/companies/{slug}/toggle", response_class=HTMLResponse)
async def companies_toggle(request: Request, slug: str, enabled: bool = Form(False)):
    if await _current_user(request) is None:
        return _login_redirect()
    async with connect() as conn:
        await q.set_personio_tenant_enabled(conn, slug, bool(enabled))
    return RedirectResponse("/companies", status_code=303)


@app.post("/companies/{slug}/delete", response_class=HTMLResponse)
async def companies_delete(request: Request, slug: str):
    if await _current_user(request) is None:
        return _login_redirect()
    async with connect() as conn:
        await q.delete_personio_tenant(conn, slug)
    return RedirectResponse("/companies", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
