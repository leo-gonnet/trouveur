"""Routes, auth and templates.

Reads the database and enqueues work; never fetches, never sweeps, never re-derives. Everything
shown comes from stored facets -- a template that parses a location is a second implementation
of a question ingest/derive.py already answered.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined

from trouveur.config import get_settings
from trouveur.crypto import encrypt, fingerprint
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import freshness
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.match import rerank
from trouveur.match.pipeline import profile_from_row
from trouveur.models import (
    BACKGROUND_MAX_CHARS,
    COUNTRY_NAMES,
    LANGUAGES,
    EmploymentType,
    RunTrigger,
    Seniority,
    UserState,
    WorkMode,
)
from trouveur.sources.registry import NORMALIZERS
from trouveur.web import auth
from trouveur.work import backlog

log = logging.getLogger(__name__)

BASE = Path(__file__).parent
app = FastAPI(title="Trouveur", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
# StrictUndefined, not Jinja's default: the default renders an unknown attribute as an empty
# string, so a template reading a field its query does not select gives a blank cell and a green
# suite.
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.undefined = StrictUndefined
templates.env.globals["version"] = importlib.metadata.version("trouveur")


def _posted_age(value) -> str:
    """How old a posting is, in words. On every card, because an edition is keyed on the day a
    posting became a recommendation, not the day it was published."""
    if value is None:
        return "date not stated"
    days = (datetime.now(UTC) - value).days
    if days <= 0:
        return "posted today"
    if days == 1:
        return "posted yesterday"
    return f"posted {days} days ago"


templates.env.filters["posted_age"] = _posted_age


def _lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


class UnknownChoice(ValueError):
    pass


class FieldTooLong(ValueError):
    pass


def _capped(raw: str, limit: int, what: str) -> str:
    """Refuse an over-long free-text field rather than truncating it to half a sentence."""
    value = raw.strip()
    if len(value) > limit:
        raise FieldTooLong(f"{what} is limited to {limit} characters; this one is {len(value)}.")
    return value


def _choices(raw: list[str], allowed: Iterable[str], what: str) -> list[str]:
    """Keep only values the form offered. An off-list value that reached the table used to 500
    the profile page and the match run for that user until someone edited the row."""
    allowed = set(allowed)
    values = [item.strip() for item in raw if item.strip()]
    for value in values:
        if value not in allowed:
            raise UnknownChoice(f"Unknown {what}: {value!r}.")
    return list(dict.fromkeys(values))


def _decimal(raw: str, default: Decimal) -> Decimal:
    try:
        return Decimal(raw.strip() or "0")
    except (InvalidOperation, ValueError):
        return default


# Public by opt-in, never by omission: the failure mode of forgetting about auth is "locked".
PUBLIC_PATHS = frozenset({"/", "/login", "/logout", "/healthz"})
PUBLIC_PREFIXES = ("/static/",)


def _session(request: Request) -> dict | None:
    return auth.read_session(get_settings(), request)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Enforce authentication before anything else looks at the request.

    Middleware, not a dependency: both a per-handler check and a dependency run AFTER FastAPI has
    parsed the request body, so an anonymous POST was being processed before the caller was known.
    """
    session = _session(request)
    request.state.session = session
    if session is None and not _is_public(request.url.path):
        return RedirectResponse("/login", status_code=303)
    # Set here, not per route: a route that forgot would hide the banner on exactly one page.
    request.state.needs_key = False
    if session is not None and not _is_public(request.url.path):
        async with connect() as conn:
            request.state.needs_key = not await users_q.has_credential(conn, session["uid"])
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse("/recommendations" if request.state.session else "/login", 303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if request.state.session:
        return RedirectResponse("/recommendations", 303)
    return templates.TemplateResponse(request, "login.html", {"error": None, "attempted": ""})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    settings = get_settings()
    async with connect() as conn:
        user_id, message = await auth.authenticate(conn, settings, username, password)
    if user_id is None:
        return templates.TemplateResponse(
            request, "login.html", {"error": message, "attempted": username}, status_code=401
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
async def recommendations(request: Request, edition: str = ""):
    """One day's edition. The latest by default; older ones stay reachable and never change.

    A day rather than one growing list, which had no time axis: a strong posting from three weeks
    ago outranked everything that arrived this morning until it was dismissed. There is nothing
    to press here -- matching is started by the daily scan, by a profile change and by adding a
    key, so the page is a reading list rather than a console.
    """
    session = request.state.session
    async with connect() as conn:
        profile_row = await users_q.get_profile(conn, session["uid"])
        available = await match_q.editions(conn, session["uid"])
        chosen = _chosen_edition(edition, available)
        jobs = await match_q.edition(conn, session["uid"], chosen.day) if chosen else []
    days = [row.day for row in available]
    index = days.index(chosen.day) if chosen else -1
    return templates.TemplateResponse(
        request,
        "recommendations.html",
        {
            "active": "recommendations",
            "jobs": jobs,
            "editions": available,
            "edition": chosen,
            "older": days[index + 1] if 0 <= index < len(days) - 1 else None,
            "newer": days[index - 1] if index > 0 else None,
            "paused": profile_row is not None and not profile_row.scoring_enabled,
            "profile_version": profile_row.version if profile_row else 1,
            "username": session["u"],
            "today": datetime.now(UTC).date(),
        },
    )


def _chosen_edition(requested: str, available: list):
    """The requested edition, or the latest. An unknown date falls back rather than 404s."""
    if not available:
        return None
    if requested:
        try:
            wanted = date.fromisoformat(requested)
        except ValueError:
            return available[0]
        for row in available:
            if row.day == wanted:
                return row
    return available[0]


async def _queue_match(conn, user_id: int) -> None:
    """Ask the runner to match this user. One at a time: a second would re-read the same rows
    on the same key and bill for it. The web app never matches, it only enqueues."""
    if await admin_q.pending_match_run(conn, user_id) is None:
        await admin_q.enqueue_run(conn, trigger=RunTrigger.MANUAL, match_user_id=user_id)


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
    return templates.TemplateResponse(
        request, "_state.html", {"job_id": job_id, "current": parsed.value}
    )


# UNKNOWN is offered on purpose: a hard filter drops every posting whose facet is unstated the
# moment it is set, and this is how a user keeps them.
_OPTIONS = {
    enum: [
        (member.value, "not stated" if member == "unknown" else member.replace("_", " "))
        for member in enum
    ]
    for enum in (WorkMode, Seniority, EmploymentType)
}


@app.get("/profile", response_class=HTMLResponse)
async def profile_form(request: Request):
    session = request.state.session
    async with connect() as conn:
        row = await users_q.get_profile(conn, session["uid"])
        profile = profile_from_row(row)
        # Priced before the edit, not billed after it.
        pending = await match_q.count_pending_rerank(conn, session["uid"], profile.version + 1)
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "active": "profile",
            "profile": profile,
            "rescore_estimate": pending,
            "username": session["u"],
            "country_names": COUNTRY_NAMES,
            "language_names": LANGUAGES,
            "background_max_chars": BACKGROUND_MAX_CHARS,
            "work_mode_options": _OPTIONS[WorkMode],
            "seniority_options": _OPTIONS[Seniority],
            "employment_type_options": _OPTIONS[EmploymentType],
        },
    )


@app.post("/profile", response_class=HTMLResponse)
async def profile_save(
    request: Request,
    title: str = Form(""),
    years_experience: int = Form(0),
    objectives: str = Form(""),
    background: str = Form(""),
    languages: str = Form(""),
    must_have: str = Form(""),
    keywords: str = Form(""),
    countries: str = Form(""),
    cities: str = Form(""),
    work_modes: Annotated[list[str] | None, Form()] = None,
    seniorities: Annotated[list[str] | None, Form()] = None,
    employment_types: Annotated[list[str] | None, Form()] = None,
    min_salary_eur_year: str = Form("0"),
):
    session = request.state.session
    try:
        values = {
            "title": title.strip(),
            "years_experience": max(0, years_experience),
            "objectives": objectives.strip(),
            "background": _capped(background, BACKGROUND_MAX_CHARS, "Background"),
            "languages": _choices(_lines(languages), LANGUAGES, "language"),
            "must_have": _lines(must_have),
            "keywords": _lines(keywords),
            "countries": _choices(_lines(countries), COUNTRY_NAMES, "country"),
            "cities": _lines(cities),
            "work_modes": _choices(work_modes or [], WorkMode, "work mode"),
            "seniorities": _choices(seniorities or [], Seniority, "seniority"),
            "employment_types": _choices(
                employment_types or [], EmploymentType, "employment type"
            ),
            "min_salary_eur_year": _decimal(min_salary_eur_year, Decimal(0)),
        }
    except (UnknownChoice, FieldTooLong) as exc:
        return HTMLResponse(str(exc), status_code=400)

    form = await request.form()
    async with connect() as conn:
        current = await users_q.get_profile(conn, session["uid"])
        rescore = users_q.changes_scoring(current, values)
        today = datetime.now(UTC).date()
        # Today's edition is the only one a save may replace, and only with the user's word for
        # it. Every older edition is a published record and is never touched, so there is
        # nothing to ask about on a day that has not been published yet.
        replacing = await match_q.edition_size(conn, session["uid"], today) if rescore else 0
        if replacing and form.get("confirm") != "yes":
            pending = await match_q.count_pending_rerank(
                conn, session["uid"], (current.version if current else 0) + 1
            )
            return templates.TemplateResponse(
                request,
                "profile_confirm.html",
                {
                    "active": "profile",
                    "username": session["u"],
                    "replacing": replacing,
                    "rescore_estimate": pending,
                    # Re-posted verbatim, so nothing the user typed is lost on the way through
                    # this page and no field list has to be kept in step with the form.
                    "fields": [
                        (key, value)
                        for key, value in form.multi_items()
                        if key != "confirm" and isinstance(value, str)
                    ],
                },
            )
        version, rescore = await users_q.save_profile(conn, session["uid"], values)
        if rescore:
            await _queue_match(conn, session["uid"])
    log.info(
        "profile saved for user %s (version %d, rescore=%s)", session["uid"], version, rescore
    )
    return RedirectResponse(f"/profile?saved=1&rescore={int(rescore)}", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request):
    session = request.state.session
    async with connect() as conn:
        credential = await users_q.get_credential(conn, session["uid"])
        profile = profile_from_row(await users_q.get_profile(conn, session["uid"]))
        spend = await users_q.month_spend(conn, session["uid"])
        history = await users_q.spend_history(conn, session["uid"])
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "model": settings.default_llm_model,
            "provider": settings.default_llm_provider or "",
            "rerank_limit": rerank.RERANK_LIMIT,
            "monthly_budget_usd": settings.monthly_budget_usd,
            "profile": profile,
            # Never the key itself, not even to the user who set it.
            "credential": credential,
            "spend": spend,
            "history": history,
            "username": session["u"],
        },
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request, api_key: str = Form("")):
    """Store a key. The ceiling is not a form field: it is an installation setting, copied onto
    the credential here so the batch check has it on the row it already reads."""
    session = request.state.session
    settings = get_settings()
    async with connect() as conn:
        key = api_key.strip()
        # Empty means "leave the stored key alone", not "delete it".
        if not key:
            return RedirectResponse("/settings?saved=1", status_code=303)
        existing = await users_q.get_credential(conn, session["uid"])
        await users_q.save_credential(
            conn,
            session["uid"],
            api_key_encrypted=encrypt(key),
            api_key_fingerprint=fingerprint(key),
            model=settings.default_llm_model,
            provider_pin=settings.default_llm_provider,
            # Replacing a key is not a reason to forget the ceiling the user set on it; the
            # installation default is the starting value for a FIRST key only.
            monthly_budget_usd=(
                existing.monthly_budget_usd if existing else settings.monthly_budget_usd
            ),
        )
        # Usually the last step of setting up, and the first thing that makes scoring
        # possible at all. Without this the page stays empty until the next daily scan.
        await _queue_match(conn, session["uid"])
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/scoring", response_class=HTMLResponse)
async def settings_scoring_save(
    request: Request,
    scoring_enabled: str = Form(""),
    monthly_budget_usd: str = Form(""),
):
    """The switch and the ceiling it governs. Neither is a SCORING_FIELD, so saving here never
    bumps the profile version or bills a re-score."""
    session = request.state.session
    enabled = scoring_enabled == "on"
    async with connect() as conn:
        previous = await users_q.get_profile(conn, session["uid"])
        was_enabled = previous.scoring_enabled if previous else True
        await users_q.save_profile(conn, session["uid"], {"scoring_enabled": enabled})

        # The form disables the ceiling while scoring is off, and a disabled input submits
        # nothing at all -- so an absent value means "keep it", never "reset it to the default".
        # Refused outright while scoring is off, rather than only hidden in the markup.
        if enabled and monthly_budget_usd.strip():
            credential = await users_q.get_credential(conn, session["uid"])
            if credential is not None:
                ceiling = _decimal(monthly_budget_usd, credential.monthly_budget_usd)
                await users_q.update_budget(conn, session["uid"], max(ceiling, Decimal(0)))

        # Only the switch turning back on is a reason to run; editing the ceiling is not.
        if enabled and not was_enabled:
            await _queue_match(conn, session["uid"])
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/delete-key")
async def settings_delete_key(request: Request):
    session = request.state.session
    async with connect() as conn:
        await users_q.delete_credential(conn, session["uid"])
    return RedirectResponse("/settings?deleted=1", status_code=303)


@app.get("/how-it-works", response_class=HTMLResponse)
async def how_it_works(request: Request):
    return templates.TemplateResponse(
        request,
        "how_it_works.html",
        {"active": "how_it_works", "username": request.state.session["u"]},
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    session = request.state.session
    async with connect() as conn:
        overview = await admin_q.corpus_overview(conn)
        coverage = await admin_q.derived_coverage(
            conn, freshness.fresh_since(get_settings().retrieval_horizon_days)
        )
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
