"""Routes, auth and templates.

Reads the database and enqueues work; never fetches, never sweeps, never re-derives. Everything
shown comes from stored facets -- a template that parses a location is a second implementation
of a question ingest/derive.py already answered.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
import subprocess
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined

from trouveur import clock, coverage
from trouveur.config import get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import freshness
from trouveur.db.queries import match as match_q
from trouveur.db.queries import quality as quality_q
from trouveur.db.queries import reports as reports_q
from trouveur.db.queries import users as users_q
from trouveur.ingest import places
from trouveur.match.pipeline import profile_from_row
from trouveur.models import (
    BACKGROUND_MAX_CHARS,
    COUNTRY_NAMES,
    DEFAULT_RADIUS_KM,
    LANGUAGES,
    MAX_RADIUS_KM,
    MIN_RADIUS_KM,
    RunTrigger,
    UserState,
)
from trouveur.sources.registry import NORMALIZERS
from trouveur.web import auth, linechart, outcome, quality, timeline

log = logging.getLogger(__name__)

BASE = Path(__file__).parent
app = FastAPI(title="Trouveur", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
# StrictUndefined, not Jinja's default: the default renders an unknown attribute as an empty
# string, so a template reading a field its query does not select gives a blank cell and a green
# suite.
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.undefined = StrictUndefined
def _localtime(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """A stored UTC instant, on the reader's wall clock. Every displayed time goes through this:
    rendered raw, a scan that ran at 09:00 in Vienna reads 07:00 and says nothing about why."""
    if value is None:
        return "\u2014"
    return clock.to_local(value).strftime(fmt)


templates.env.filters["localtime"] = _localtime
templates.env.globals["version"] = importlib.metadata.version("trouveur")
templates.env.globals["timezone"] = get_settings().timezone
templates.env.globals["min_password_chars"] = auth.MIN_PASSWORD_CHARS


def _asset(name: str) -> str:
    """A static file's URL, carrying its mtime, so an edit reaches the reader.

    StaticFiles answers with an ETag, so a browser that ASKS gets the new bytes -- but nothing
    makes it ask. The URL never changed, so Chrome kept serving a cached stylesheet and a CSS fix
    looked like it had not been applied until somebody knew to hard-refresh. Changing the URL is
    the one thing every browser honours.

    Stat per render rather than read once at import: `--reload` watches Python files, so editing
    only CSS does not restart the dev server, and a version frozen at import would be the stale
    thing it is here to prevent. One stat per page is nothing at this size.
    """
    try:
        stamp = int((BASE / "static" / name).stat().st_mtime)
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={stamp}"


templates.env.globals["asset"] = _asset

REPO_URL = "https://github.com/leo-gonnet/trouveur"
_SHA = re.compile(r"\A[0-9a-f]{7,40}\Z")


def _deployed_commit() -> str:
    """Which commit this instance is running.

    `TROUVEUR_TAG` is what deploy.yml writes into `.env` on the host AND what compose resolves
    the image tag from, so the footer cannot drift from the running code: a wrong commit here
    would mean a wrong container. The package version is static and says nothing about a deploy.

    A local run has no tag, so it falls back to the working tree. The image has no `.git`
    (`.dockerignore` excludes it), which is why this is a fallback and not the source.
    """
    tag = get_settings().trouveur_tag.strip()
    if tag:
        return tag
    try:
        result = subprocess.run(
            ["git", "-C", str(BASE.parent.parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def commit_links(raw: str) -> tuple[str, str]:
    """What the footer shows for `raw`, and where it links -- no link when there is nowhere real.

    A tag that is not a commit (`latest`, on a fresh install) is shown as it is: "latest" is the
    honest answer to "which commit is this?", and linking it would go to a 404.
    """
    if _SHA.match(raw):
        return raw[:7], f"{REPO_URL}/commit/{raw}"
    return raw, ""


templates.env.globals["repo_url"] = REPO_URL
templates.env.globals["commit"], templates.env.globals["commit_url"] = commit_links(
    _deployed_commit()
)


def _posted_age(value) -> str:
    """How old a posting is, in words. On every card, because an edition is keyed on the day a
    posting became a recommendation, not the day it was published.

    Calendar days in the reader's zone, not elapsed hours: measured as elapsed time, something
    posted at 23:00 last night was "posted today" all through this morning, because only eleven
    hours had passed.
    """
    if value is None:
        return "date not stated"
    days = (clock.today() - clock.to_local(value).date()).days
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


# Enough for "trial", "topped up after the September run", not for an essay.
GRANT_NOTE_MAX_CHARS = 200


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


def _places(raw: list[str]) -> list[int]:
    """Towns by GeoNames id, the only form the filter can match. A typed name never gets here:
    the form posts the id of the suggestion that was picked."""
    ids = []
    for value in raw:
        if not value.isdigit() or places.get(int(value)) is None:
            raise UnknownChoice(f"Unknown city: {value!r}.")
        ids.append(int(value))
    return list(dict.fromkeys(ids))


# Every field this parses is an amount of money -- a daily ceiling, a credit grant -- so nine
# digits either side of the point is already absurd for both. The bound is not fussiness:
# `1e999999999` is a perfectly FINITE Decimal that asyncpg refuses to encode ("exponent is too
# large"), which is the same 500 one layer further down, raised from inside the write.
_MONEY_DIGITS = 9


def _decimal(raw: str, default: Decimal) -> Decimal:
    """A money field from a form, or `default` if it is not a usable amount.

    Three ways a string gets past `Decimal()` without raising, and all three used to reach a
    column: `nan`, which raises InvalidOperation from the first `>` or `max()` that touches it --
    an uncaught 500 on a form meant to fall back; `inf`, which is worse than a crash because it is
    accepted as a spending ceiling and silently removes the cap; and an absurd exponent, which
    fails at the driver instead.
    """
    try:
        value = Decimal(raw.strip() or "0")
    except (InvalidOperation, ValueError):
        return default
    # `adjusted()` reads the exponent off the tuple and does no arithmetic, which matters because
    # `abs()` on one of these raises Overflow before any comparison of ours could reject it.
    if not value.is_finite() or not -_MONEY_DIGITS <= value.adjusted() <= _MONEY_DIGITS:
        return default
    # In range but written with a scale no money column has a use for: 0.00001 is not a rate here.
    return value.quantize(Decimal("0.0001")) if value.as_tuple().exponent < -4 else value


# Public by opt-in, never by omission: the failure mode of forgetting about auth is "locked".
PUBLIC_PATHS = frozenset({"/", "/login", "/logout", "/healthz"})
PUBLIC_PREFIXES = ("/static/",)

# Admin by prefix rather than per route, for the same reason the session check is middleware: a
# per-route check is a line somebody forgets on the next route, and the route that leaks is always
# the newest one. Operations can cancel the shared run, so it is not one reader's to stop; Users
# creates accounts and grants credit.
ADMIN_PREFIXES = ("/admin", "/users")


def _session(request: Request) -> dict | None:
    return auth.read_session(get_settings(), request)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _is_admin_only(path: str) -> bool:
    return path.startswith(ADMIN_PREFIXES)


def _auth_redirect(request: Request, url: str) -> Response:
    """Send an htmx caller to `url` as a browser NAVIGATION, not as content to swap in.

    htmx follows a 303 transparently, so a session that lapsed between loading the page and
    pressing a button came back 200 with the login page's HTML, and htmx pasted that into
    whatever the button targeted -- a whole login form inside a 28px span, with the reader still
    looking at a list they can no longer act on. `HX-Redirect` is the only answer htmx reads as
    "leave this page". A plain form post is unaffected and still gets the 303.
    """
    if request.headers.get("HX-Request") == "true":
        response = Response(status_code=204)
        response.headers["HX-Redirect"] = url
        return response
    return RedirectResponse(url, status_code=303)


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Enforce authentication before anything else looks at the request.

    Middleware, not a dependency: both a per-handler check and a dependency run AFTER FastAPI has
    parsed the request body, so an anonymous POST was being processed before the caller was known.
    """
    session = _session(request)
    request.state.session = session
    path = request.url.path
    if session is None and not _is_public(path):
        return _auth_redirect(request, "/login")
    # Set here, not per route: a route that forgot would hide the banner on exactly one page, and
    # the one page it hid it on would be the page that needed it.
    request.state.is_admin = False
    # Read from the row on every request rather than from the cookie, so renaming yourself changes
    # the topbar at once instead of at the next login a month later.
    request.state.display_name = ""
    request.state.email = ""
    request.state.balance_usd = None
    request.state.out_of_credit = False
    request.state.scoring_unconfigured = False
    if session is not None and not _is_public(path):
        async with connect() as conn:
            user = await users_q.get_user(conn, session["uid"])
            # Checked on every request, not only at login: a session is a signed token with a
            # 30-day life, so an admin disabling an account would otherwise revoke nothing the
            # holder of that cookie can still do -- for a month, while the Users page says
            # "cannot log in". Covers a deleted account's surviving cookie too.
            if user is None or not user.is_active:
                revoked = _auth_redirect(request, "/login")
                auth.clear_cookie(revoked)
                return revoked
            request.state.is_admin = bool(user.is_admin)
            request.state.display_name = user.display_name
            request.state.email = user.email
            if _is_admin_only(path) and not request.state.is_admin:
                return _auth_redirect(request, "/recommendations")
            balance = Decimal((await users_q.credit(conn, session["uid"])).balance_usd)
            request.state.balance_usd = balance
            request.state.out_of_credit = balance <= 0
        request.state.scoring_unconfigured = not get_settings().openrouter_api_key.strip()
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
async def login(request: Request, email: str = Form(""), password: str = Form("")):
    settings = get_settings()
    async with connect() as conn:
        user_id, message = await auth.authenticate(conn, settings, email, password)
    if user_id is None:
        return templates.TemplateResponse(
            request, "login.html", {"error": message, "attempted": email}, status_code=401
        )
    response = RedirectResponse("/recommendations", status_code=303)
    auth.set_cookie(response, settings, auth.issue_session(settings, user_id))
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_cookie(response)
    return response


# How much of an edition is read at once. A module constant so a test can shrink it and render the
# pager itself: a page test whose fixture fits on one page never executes that markup at all.
EDITION_PAGE = 50


@app.get("/recommendations", response_class=HTMLResponse)
async def recommendations(
    request: Request, edition: str = "", v: int = 0, after: str = "", before: str = ""
):
    """One edition. The latest by default; every earlier one stays reachable and never changes.

    An edition is a (day, profile version) pair rather than a day, so changing a profile adds one
    beside what you were already shown instead of replacing it.

    Paged by cursor rather than by page number, because dismissing a posting shrinks the edition
    and page numbers would then skip whatever crossed the boundary. See `match_q.edition`.

    A day rather than one growing list, which had no time axis: a strong posting from three weeks
    ago outranked everything that arrived this morning until it was dismissed. There is nothing
    to press here -- matching is started by the daily scan, by a profile change and by adding a
    key, so the page is a reading list rather than a console.
    """
    session = request.state.session
    limit = EDITION_PAGE
    forward, backward = _cursor(after), _cursor(before)
    async with connect() as conn:
        profile_row = await users_q.get_profile(conn, session["uid"])
        available = await match_q.editions(conn, session["uid"])
        chosen = _chosen_edition(edition, v, available)
        # One more than a page, so "is there another page" is answered by the rows themselves
        # rather than by a second count that could disagree with them.
        rows = (
            await match_q.edition(
                conn, session["uid"], chosen.day, chosen.profile_version,
                limit=limit + 1, after=forward, before=backward,
            )
            if chosen
            else []
        )
    spilled = len(rows) > limit
    jobs = rows[-limit:] if backward else rows[:limit]
    index = available.index(chosen) if chosen else -1
    return templates.TemplateResponse(
        request,
        "recommendations.html",
        {
            "active": "recommendations",
            "jobs": jobs,
            "editions": available,
            "edition": chosen,
            "older": available[index + 1] if 0 <= index < len(available) - 1 else None,
            "newer": available[index - 1] if index > 0 else None,
            # Reading backwards, there is always a page ahead: it is the one we came from.
            "next_cursor": _format_cursor(jobs[-1]) if jobs and (spilled or backward) else "",
            "prev_cursor": (
                _format_cursor(jobs[0]) if jobs and (forward or (backward and spilled)) else ""
            ),
            "paged": bool(forward or backward),
            "paused": profile_row is not None and not profile_row.scoring_enabled,
            "profile_version": profile_row.version if profile_row else 1,
            "today": clock.today(),
        },
    )


def _cursor(raw: str) -> tuple[int, int] | None:
    """A (score, job id) cursor as it travels in a URL. Anything unreadable starts from the top,
    which is the same thing a stale link should do."""
    score, _, job_id = raw.partition("_")
    try:
        return int(score), int(job_id)
    except ValueError:
        return None


def _format_cursor(row) -> str:
    return f"{row.llm_score}_{row.id}"


def _chosen_edition(requested: str, version: int, available: list):
    """The requested edition, or the latest. An unknown one falls back rather than 404s.

    A day alone still resolves -- to that day's newest edition -- so a link written before a
    profile change keeps working instead of breaking on the day it is most likely to be followed.
    """
    if not available:
        return None
    if not requested:
        return available[0]
    try:
        wanted = date.fromisoformat(requested)
    except ValueError:
        return available[0]
    same_day = [row for row in available if row.day == wanted]
    if not same_day:
        return available[0]
    for row in same_day:
        if row.profile_version == version:
            return row
    # Ordered newest version first by the query.
    return same_day[0]


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
        request, "job.html", {"active": "search", "job": job}
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


# How many of a reader's own reported links are listed. Long enough that nobody loses one, short
# enough to stay a list: this is a person pasting links by hand.
REPORTS_SHOWN = 25
# A URL nobody typed. Past this the box is being used for something that is not a link.
MAX_REPORT_URL_CHARS = 2000


@app.get("/elsewhere", response_class=HTMLResponse)
async def elsewhere(request: Request):
    """The reader's own reported links, and what we could tell them about each."""
    session = request.state.session
    async with connect() as conn:
        rows = await reports_q.for_user(conn, session["uid"], chunk=REPORTS_SHOWN)
    context = {
        "active": "elsewhere",
        "reports": rows,
        # Only while something is unanswered: the list polls for the answer, and a page that
        # polled for ever would ask for a list nothing can change.
        "waiting": any(row.outcome is None for row in rows),
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_elsewhere_list.html", context)
    return templates.TemplateResponse(request, "elsewhere.html", context)


@app.post("/elsewhere", response_class=HTMLResponse)
async def elsewhere_report(request: Request, url: str = Form("")):
    """Store the link and nothing else.

    No fetch and no resolving here. The web layer never goes to the network, and reading a link
    as a board is versioned work that belongs in one place: done here as well it would answer
    differently from the runner the day the rules change, and the reader would see both.

    The scheme is checked because this value is later rendered as the href of a link the reader
    presses, and `javascript:` is a scheme too.
    """
    session = request.state.session
    candidate = url.strip()
    # Splitting a URL whose netloc holds a bracket raises Invalid IPv6 URL, so a pasted
    # `https://[::1` answered 500 instead of the refusal this check was written to give. An
    # address we cannot even split is exactly the bad URL being rejected here.
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
    except ValueError:
        return RedirectResponse("/elsewhere?error=url", status_code=303)
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or len(candidate) > MAX_REPORT_URL_CHARS
    ):
        return RedirectResponse("/elsewhere?error=url", status_code=303)
    async with connect() as conn:
        await reports_q.create(conn, user_id=session["uid"], url=candidate)
    return RedirectResponse("/elsewhere?filed=1", status_code=303)


@app.get("/profile", response_class=HTMLResponse)
async def profile_form(request: Request):
    session = request.state.session
    async with connect() as conn:
        row = await users_q.get_profile(conn, session["uid"])
        profile = profile_from_row(row)
    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "active": "profile",
            "profile": profile,
            "country_names": COUNTRY_NAMES,
            "city_names": {
                place.id: place.label for place in map(places.get, profile.city_ids) if place
            },
            "radius_bounds": (MIN_RADIUS_KM, MAX_RADIUS_KM),
            "language_names": LANGUAGES,
            "background_max_chars": BACKGROUND_MAX_CHARS,
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
    city_ids: str = Form(""),
    radius_km: int = Form(DEFAULT_RADIUS_KM),
    remote_anywhere: str = Form(""),
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
            "city_ids": _places(_lines(city_ids)),
            "radius_km": min(max(radius_km, MIN_RADIUS_KM), MAX_RADIUS_KM),
            # An unticked checkbox submits nothing, so absence is False here -- unlike the
            # scoring ceiling, where a disabled input's absence means "keep what is set".
            "remote_anywhere": remote_anywhere == "yes",
            "min_salary_eur_year": _decimal(min_salary_eur_year, Decimal(0)),
        }
    except (UnknownChoice, FieldTooLong) as exc:
        return HTMLResponse(str(exc), status_code=400)

    # No confirm step: a save destroys nothing. A profile change publishes a second edition for
    # today beside the one already there, so there is nothing to ask the user's permission for.
    async with connect() as conn:
        version, rescore = await users_q.save_profile(conn, session["uid"], values)
        if rescore:
            await _queue_match(conn, session["uid"])
    log.info(
        "profile saved for user %s (version %d, rescore=%s)", session["uid"], version, rescore
    )
    return RedirectResponse(f"/profile?saved=1&rescore={int(rescore)}", status_code=303)


@app.get("/places", response_class=HTMLResponse)
async def place_options(request: Request, q: str = ""):
    return templates.TemplateResponse(
        request, "_place_options.html", {"places": places.suggest(q)}
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request):
    session = request.state.session
    async with connect() as conn:
        user = await users_q.get_user(conn, session["uid"])
        profile = profile_from_row(await users_q.get_profile(conn, session["uid"]))
        credit = await users_q.credit(conn, session["uid"])
        grants = await users_q.credit_grants(conn, session["uid"])
        today = await users_q.today_spend(conn, session["uid"])
        history = await users_q.spend_history(conn, session["uid"])
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "model": settings.default_llm_model,
            "provider": settings.default_llm_provider or "",
            "email": user.email,
            "display_name": user.display_name,
            "profile": profile,
            "credit": credit,
            "grants": grants,
            "today": today,
            "history": history,
        },
    )


@app.post("/settings/scoring", response_class=HTMLResponse)
async def settings_scoring_save(
    request: Request,
    scoring_enabled: str = Form(""),
    daily_ceiling_usd: str = Form(""),
):
    """The switch and the ceiling it governs. Neither is a SCORING_FIELD, so saving here never
    bumps the profile version or bills a re-score.

    The ceiling is a plain column with a default now, so an absent value simply keeps what is
    stored -- there is nothing to distinguish from "reset me", which is what the form's disabled
    input used to require the route to guess at.
    """
    session = request.state.session
    enabled = scoring_enabled == "on"
    async with connect() as conn:
        previous = await users_q.get_profile(conn, session["uid"])
        was_enabled = previous.scoring_enabled if previous else True
        values: dict = {"scoring_enabled": enabled}
        # Refused outright while scoring is off, rather than only hidden in the markup.
        if enabled and daily_ceiling_usd.strip():
            ceiling = _decimal(daily_ceiling_usd, Decimal(previous.daily_ceiling_usd))
            values["daily_ceiling_usd"] = max(ceiling, Decimal(0))
        await users_q.save_profile(conn, session["uid"], values)

        # Only the switch turning back on is a reason to run; editing the ceiling is not.
        if enabled and not was_enabled:
            await _queue_match(conn, session["uid"])
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/account", response_class=HTMLResponse)
async def settings_account(
    request: Request, display_name: str = Form(""), digest_enabled: str = Form("")
):
    """The name the UI shows, and whether the day's edition is also mailed.

    The address itself is not editable here: it is the login, and changing it is the one edit that
    can lock somebody out of their own account by a typo. An admin changes it on the Users page,
    where there is somebody else to undo it.
    """
    session = request.state.session
    async with connect() as conn:
        user = await users_q.get_user(conn, session["uid"])
        await users_q.set_display_name(
            conn, session["uid"], auth.display_name_for(display_name, user.email)
        )
        await users_q.save_profile(
            conn, session["uid"], {"digest_enabled": digest_enabled == "on"}
        )
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/password", response_class=HTMLResponse)
async def settings_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    repeat_password: str = Form(""),
):
    """Change your own password. The current one is required, so a borrowed session cannot lock
    the owner out of their account."""
    session = request.state.session
    async with connect() as conn:
        user = await users_q.get_user(conn, session["uid"])
        if user is None or not auth.verify_password(user.password_hash, current_password):
            return RedirectResponse("/settings?password=wrong", status_code=303)
        if len(new_password) < auth.MIN_PASSWORD_CHARS:
            return RedirectResponse("/settings?password=short", status_code=303)
        if new_password != repeat_password:
            return RedirectResponse("/settings?password=mismatch", status_code=303)
        await users_q.set_password(conn, session["uid"], auth.hash_password(new_password))
    log.info("password changed for user %s", session["uid"])
    return RedirectResponse("/settings?password=changed", status_code=303)


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    """The admin's people page: who exists, what credit they hold, and a way to add both.

    Managing accounts is a web action while managing the CRAWL SET stays a CLI one, and the
    difference is who pays for the decision: enlarging the crawl costs every user politeness
    budget and bill, while an account costs only the credit this page grants it.
    """
    return await _users_response(request)


@app.post("/users", response_class=HTMLResponse)
async def users_create(
    request: Request,
    email: str = Form(""),
    display_name: str = Form(""),
    credit: str = Form(""),
):
    """Create an account and show its password once.

    Rendered directly rather than redirected to, because the only other way to carry a password to
    the next page is a URL -- and a URL is in the browser's history, the proxy's log and the
    Referer of whatever the reader clicks next.
    """
    session = request.state.session
    try:
        login = auth.clean_email(email)
    except auth.InvalidEmail as exc:
        return await _users_response(request, error=str(exc))
    async with connect() as conn:
        if await users_q.get_user_by_email(conn, login):
            return await _users_response(request, error=f"{login} already has an account.")

        password = auth.generate_password()
        user_id = await users_q.create_user(
            conn,
            login,
            auth.hash_password(password),
            auth.display_name_for(display_name, login),
            is_admin=False,
        )
        opening = max(_decimal(credit, Decimal(0)), Decimal(0))
        if opening > 0:
            await users_q.grant_credit(
                conn, user_id, amount_usd=opening, granted_by=session["uid"], note="opening credit"
            )
    # The id, not the address: a log line is not the place to accumulate people's emails.
    log.info("user %s created account %s", session["uid"], user_id)
    return await _users_response(request, new_login=(login, password))


@app.post("/users/{user_id}/credit")
async def users_grant(
    request: Request, user_id: int, amount_usd: str = Form(""), note: str = Form("")
):
    """Top up. A grant is a row, so two top-ups add up and nothing overwrites a total."""
    session = request.state.session
    amount = _decimal(amount_usd, Decimal(0))
    if amount == 0:
        return RedirectResponse("/users", status_code=303)
    async with connect() as conn:
        try:
            reason = _capped(note, GRANT_NOTE_MAX_CHARS, "The note")
        except FieldTooLong as exc:
            return await _users_response(request, error=str(exc))
        await users_q.grant_credit(
            conn, user_id, amount_usd=amount, granted_by=session["uid"], note=reason
        )
        # Credit is what stops a run, so granting some is a reason to start one: without this the
        # person waits for the next daily scan to see anything they were just paid for.
        if amount > 0:
            await _queue_match(conn, user_id)
    log.info("user %s granted $%s to user %s", session["uid"], amount, user_id)
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/active")
async def users_set_active(request: Request, user_id: int, is_active: str = Form("")):
    session = request.state.session
    if user_id == session["uid"]:
        # Nothing else can re-enable them: there is no route that does not require a session.
        return RedirectResponse("/users", status_code=303)
    async with connect() as conn:
        await users_q.set_active(conn, user_id, is_active=is_active == "on")
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/delete", response_class=HTMLResponse)
async def users_delete(request: Request, user_id: int, confirm_email: str = Form("")):
    """Erase an account for good: the person, their profile, their scores and their editions.

    Disabling is the reversible answer and stays the usual one. This is here for the other case --
    somebody who is gone, whose career data we have no reason to keep -- because the repo is
    public and the database is the only place that data ever lives, so there has to be a way to
    take it out that is not a psql prompt.

    Guarded by typing the address rather than by a dialog: a browser confirm() is a click away
    from the button that opened it and reads the same on the right row as on the wrong one, and
    the rows on this page differ only by a name. Nothing here is recoverable, so the check is
    made on the server, where no missing script can skip it.
    """
    session = request.state.session
    if user_id == session["uid"]:
        # Said rather than silently ignored: a delete that answers with the page it was posted
        # from, unchanged, reads exactly like a delete that worked.
        return await _users_response(request, error="You cannot delete your own account.")
    async with connect() as conn:
        user = await users_q.get_user(conn, user_id)
        if user is None:
            return await _users_response(request, error="That account no longer exists.")
        # Folded, not parsed: the stored address is already lower case, and what is being asked
        # for here is a match with the row in front of the admin, not a valid address.
        if confirm_email.strip().lower() != user.email:
            return await _users_response(
                request,
                error=f"Type {user.email} exactly to delete that account. Nothing was deleted.",
            )
        await users_q.delete_user(conn, user_id)
    # The id, not the address: the point of deleting an account is not to keep its email in a log.
    log.warning("user %s deleted account %s", session["uid"], user_id)
    return await _users_response(request, deleted=user.display_name)


async def _users_response(
    request: Request,
    *,
    error: str = "",
    new_login: tuple[str, str] | None = None,
    deleted: str = "",
) -> HTMLResponse:
    async with connect() as conn:
        users = await users_q.list_users_with_credit(conn)
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "active": "users",
            "admin_page": "users",
            "users": users,
            "error": error,
            # The generated password, shown exactly once: in this response and nowhere after it.
            "new_login": new_login,
            # Who just went. The row is gone from the table below, so without this the page
            # answers a delete by looking like nothing was asked.
            "deleted": deleted,
            "user_id": request.state.session["uid"],
        },
    )


@app.get("/how-it-works", response_class=HTMLResponse)
async def how_it_works(request: Request):
    return templates.TemplateResponse(
        request,
        "how_it_works.html",
        {"active": "how_it_works"},
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
        running = []
        if active is not None and active.started_at:
            running = await admin_q.running_sweeps(conn, since=active.started_at)
        queues = await admin_q.queue_depth(conn)
        typical = await admin_q.typical_sweep_seconds(conn)
        done_sources = set()
        remaining_estimate = None
        unknown_sources = 0
        if active is not None and active.sources_total and active.started_at:
            done_sources = {
                row.source
                for row in await admin_q.recent_sweeps(conn, limit=40)
                if row.started_at >= active.started_at
            }
            pending = max(active.sources_total - (active.sources_done or 0), 0)
            if pending:
                known = [
                    seconds for name, seconds in typical.items() if name not in done_sources
                ]
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


def _admin_page(request: Request, page: str, context: dict) -> HTMLResponse:
    return templates.TemplateResponse(
        request, f"admin_{page}.html", {"active": "admin", "admin_page": page, **context}
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    """Is everything OK: one verdict per area, each linking to the page that explains it.

    Built from the same queries as those pages, never a second count of the same thing. The
    coverage report is left out: it is the one slow query, and this page must always load.
    """
    now = datetime.now(UTC)
    async with connect() as conn:
        health = await admin_q.source_health(conn)
        _, tenant_totals = await admin_q.tenants_needing_attention(conn, limit=0)
        queues = await admin_q.queue_depth(conn)
        overview = await admin_q.system_overview(
            conn, freshness.fresh_since(get_settings().retrieval_horizon_days)
        )
        active = await admin_q.active_run(conn)
        _, _, unknown_totals = await _unknown_location(conn, now)
    unknown_usual, unknown_last = outcome.usual_and_last(unknown_totals)
    last_runs = await _last_runs()
    finished = [
        run for run in last_runs["runs"]
        if run.finished_at is not None and run.match_user_id is None
    ]
    return _admin_page(
        request,
        "overview",
        {
            "active_run": active,
            "last_run": finished[0] if finished else None,
            "timeline": last_runs["timeline"],
            "steps": last_runs["steps"],
            "health": health,
            "sources": sorted(NORMALIZERS),
            "tenant_totals": tenant_totals,
            "queues": queues,
            "overview": overview,
            "horizon_days": get_settings().retrieval_horizon_days,
            "unknown_usual": unknown_usual,
            "unknown_last": unknown_last,
            "unknown_jumped": unknown_usual is not None
            and unknown_last > UNKNOWN_JUMP * unknown_usual,
        },
    )


UNKNOWN_JUMP = 1.5


async def _unknown_location(conn, now: datetime):
    rows = await admin_q.unknown_location_history(conn, now - timedelta(days=quality.DAYS + 1))
    days = quality.days(now)
    nights = outcome.nightly(rows, days, lambda row: row.unknown_location)
    totals = [sum(night.values()) if night is not None else None for night in nights]
    return days, nights, totals


@app.get("/admin/runs", response_class=HTMLResponse)
async def admin_runs(request: Request):
    return _admin_page(request, "runs", await _runs_cards())


@app.get("/admin/sources", response_class=HTMLResponse)
async def admin_sources(request: Request):
    now = datetime.now(UTC)
    days = quality.days(now)
    async with connect() as conn:
        health = await admin_q.source_health(conn)
        sweeps = await admin_q.sweep_history(conn, now - timedelta(days=quality.DAYS + 1))
        tenants, tenant_totals = await admin_q.tenants_needing_attention(
            conn, limit=FAILING_BOARDS_SHOWN
        )
        failing_by_source = await admin_q.failing_boards_by_source(conn)
        crawl_set = await coverage.tenants(conn)
    by_source: dict[str, list] = {}
    for row in sweeps:
        by_source.setdefault(row.source, []).append(row)
    return _admin_page(
        request,
        "sources",
        {
            "health": health,
            "sparks": {
                source: linechart.spark(
                    outcome.nightly(rows, days, lambda row: row.documents_seen)
                )
                for source, rows in by_source.items()
            },
            "spark_nights": len(days),
            "failing_by_source": failing_by_source,
            "tenants": tenants,
            "tenant_totals": tenant_totals,
            "crawl_set": crawl_set,
            "sources": sorted(NORMALIZERS),
        },
    )


FAILING_BOARDS_SHOWN = 5


@app.get("/admin/quality", response_class=HTMLResponse)
async def admin_quality(request: Request, reader: str = "all", good: int = quality.GOOD):
    """The scores of the last nights and the recall checks, for every reader at once or one.

    Readers with neither a score nor a check in the window are left out of the choice: there is
    nothing of theirs to show.
    """
    now = datetime.now(UTC)
    since = now - timedelta(days=quality.DAYS + 1)
    good = min(max(good, 1), 100)
    readers = []
    async with connect() as conn:
        for user in await users_q.list_users(conn):
            if not user.is_active:
                continue
            checks = await quality_q.checks(conn, user.id, since)
            scores = await quality_q.scores(conn, user.id, since)
            if not checks and not scores:
                continue
            job_ids = {row.job_id for row in checks if row.job_id is not None}
            jobs = {
                row.job_id: row for row in await quality_q.needle_jobs(conn, user.id, job_ids)
            }
            readers.append(
                {
                    "user": user,
                    "scores": quality.score_nights(scores, now, good),
                    "recall": quality.recall_nights(checks, jobs, now),
                    "jobs": jobs,
                }
            )
    shown = [row for row in readers if reader == "all" or str(row["user"].id) == reader]
    recall = quality.pooled([row["recall"] for row in shown]) if shown else []
    scores = quality.median_nights([row["scores"] for row in shown]) if shown else []
    days = quality.days(now)
    if not shown:
        return _admin_page(
            request, "quality", {"readers": readers, "reader": reader, "good": good, "scores": []}
        )
    start = linechart.trim(
        days,
        [night.scored for night in scores],
        [night.needles for night in recall],
    )
    days, scores, recall = days[start:], scores[start:], recall[start:]

    def wide(series, **options):
        return linechart.build(days, series, width=1200, height=180, **options)

    return _admin_page(
        request,
        "quality",
        {
            "readers": readers,
            "reader": reader,
            "good": good,
            "scores": scores,
            "recall": recall,
            "good_chart": wide(
                [("good", "good jobs", [night.good for night in scores])]
            ),
            "share_chart": wide(
                [("share", "share good", [night.share_good for night in scores])],
                percent=True,
            ),
            "dismissed_chart": wide(
                [("dismissed", "good, dismissed", [night.dismissed_good for night in scores])],
                ranges=[night.dismissed_range for night in scores],
                percent=True,
            ),
            "recall_chart": wide(
                [
                    ("kept", "kept jobs", [night.needles for night in recall]),
                    ("found", "found", [
                        night.found if night.needles is not None else None for night in recall
                    ]),
                ],
            ),
            "found_week": quality.last_week(recall),
            "agreement_nights": quality.AGREEMENT_NIGHTS,
        },
    )


@app.get("/admin/coverage", response_class=HTMLResponse)
async def admin_coverage(request: Request):
    async with connect() as conn:
        countries = await admin_q.facet_breakdown(conn)
        days, unknown, totals = await _unknown_location(conn, datetime.now(UTC))
        recall, misses = await coverage.recall(conn)
        parts = {
            "recall": recall,
            "misses": misses,
            "reports": await coverage.reports(conn),
            "leads": await coverage.leads(conn),
        }
    start = linechart.trim(days, totals)
    return _admin_page(
        request,
        "coverage",
        {
            "countries": countries,
            "coverage": parts,
            "recall_days": coverage.RECALL_DAYS,
            "linkedin_on": get_settings().linkedin_max_requests > 0,
            "horizon_days": get_settings().retrieval_horizon_days,
            "unknown": unknown[start:],
            "unknown_chart": linechart.build(
                days[start:],
                [("unknown", "unknown location", totals[start:])],
                width=1200,
                height=180,
            ),
        },
    )


@app.post("/admin/run/{run_id}/cancel")
async def admin_cancel_run(request: Request, run_id: int):
    """Ask a run to stop. A queued one ends at once; a running one stops between sources."""
    async with connect() as conn:
        await admin_q.request_cancel(conn, run_id)
    return RedirectResponse("/admin/runs", status_code=303)


@app.get("/admin/coverage/areas", response_class=HTMLResponse)
async def admin_coverage_areas(request: Request):
    """Fetched after the page rather than with it: one filtered pass over the open corpus per
    area, and the location filter is the most expensive predicate the system has."""
    horizon_days = get_settings().retrieval_horizon_days
    async with connect() as conn:
        parts = {
            "areas": await coverage.by_area(conn, horizon_days),
            "blocked": await coverage.blocked(conn),
        }
    return templates.TemplateResponse(request, "_coverage_areas.html", {"coverage": parts})


RUNS_SHOWN = 10


async def _last_runs() -> dict:
    async with connect() as conn:
        runs = await admin_q.recent_runs(conn, limit=RUNS_SHOWN)
    now = datetime.now(UTC)
    return {
        "runs": runs,
        "step_seconds": {run.id: timeline.step_seconds(run, now) for run in runs},
        "outcomes": {run.id: outcome.of(run.report) for run in runs},
        "timeline": timeline.build(runs, now),
        "steps": [step.value for step in timeline.STEPS],
    }


async def _runs_cards() -> dict:
    settings = get_settings()
    return {
        **await _last_runs(),
        **await _run_status(),
        "run_daily": settings.run_daily,
        "run_time": f"{settings.run_hour:02d}:{settings.run_minute:02d}",
    }


@app.get("/admin/runs/live", response_class=HTMLResponse)
async def admin_runs_fragment(request: Request):
    return templates.TemplateResponse(request, "_admin_runs.html", await _runs_cards())


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
