"""JobSpy wrapper — LinkedIn, Indeed, Glassdoor, Google.

Optional (`uv sync --extra jobspy`); yields nothing when absent rather than failing.

Runs last and narrow: every board caps a search near 1000 results and LinkedIn rate-limits around
page 10 from a single IP, which is all this host has. JobSpy is synchronous, so it runs in a
worker thread rather than blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

from trouveur.models import Job, SalaryPeriod

log = logging.getLogger(__name__)

DEFAULT_SITES = ("indeed", "google")
_COUNTRY = {"AT": "austria", "DE": "germany", "CH": "switzerland"}


class JobSpySource:
    name = "jobspy"

    def __init__(
        self, keywords: list[str], countries: list[str] | None = None,
        sites: tuple[str, ...] = DEFAULT_SITES, results_per_search: int = 25,
    ) -> None:
        self.keywords = keywords
        self.countries = countries or ["AT", "DE"]
        self.sites = list(sites)
        self.results_per_search = results_per_search

    async def fetch(self, client, since: datetime | None) -> AsyncIterator[Job]:
        try:
            from jobspy import scrape_jobs
        except ImportError:
            log.info("python-jobspy is not installed; skipping. `uv sync --extra jobspy`")
            return

        hours_old = None
        if since is not None:
            hours_old = max(1, int((datetime.now(UTC) - since).total_seconds() // 3600))

        for country in self.countries:
            for keyword in self.keywords:
                try:
                    frame = await asyncio.to_thread(
                        scrape_jobs,
                        site_name=self.sites,
                        search_term=keyword,
                        country_indeed=_COUNTRY.get(country, "germany"),
                        results_wanted=self.results_per_search,
                        hours_old=hours_old,
                    )
                except Exception as exc:  # noqa: BLE001 - scraping is best-effort by nature
                    log.warning("jobspy: %r/%s failed: %s", keyword, country, exc)
                    continue
                if frame is None or frame.empty:
                    continue
                for record in frame.to_dict("records"):
                    job = _to_job(record, country)
                    if job is not None:
                        yield job


def _to_job(record: dict, country: str) -> Job | None:
    title = _text(record.get("title"))
    url = _text(record.get("job_url"))
    if not title or not url:
        return None

    interval = str(record.get("interval") or "").upper()
    period = {
        "YEARLY": SalaryPeriod.YEAR, "MONTHLY": SalaryPeriod.MONTH,
        "WEEKLY": SalaryPeriod.WEEK, "DAILY": SalaryPeriod.DAY, "HOURLY": SalaryPeriod.HOUR,
    }.get(interval, SalaryPeriod.UNKNOWN)

    return Job(
        source="jobspy",
        source_native_id=_text(record.get("id")) or url,
        url=url,
        title=title,
        company=_text(record.get("company")),
        location_city=_text(record.get("location")),
        location_country=country,
        remote=bool(record.get("is_remote")) if record.get("is_remote") is not None else None,
        salary_min=_decimal(record.get("min_amount")),
        salary_max=_decimal(record.get("max_amount")),
        salary_period=period,
        posted_at=_datetime(record.get("date_posted")),
        description=_text(record.get("description")),
        raw={"site": _text(record.get("site"))},
    )


def _text(value) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text if text and text.lower() != "nan" else None


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return None if parsed.is_nan() else parsed


def _datetime(value) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
