"""Jobicy payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Jobicy prefixes every field with `job`, so the title is `jobTitle` and the description is
`jobDescription`. Reading `title` yields None and the posting is dropped as untitled.

`jobGeo` names where the role may be worked from ("USA", "Anywhere"), not an office address. It is
passed through unparsed, as with every other source's location string.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from trouveur.models import (
    CanonicalJob,
    Location,
    SalaryPeriod,
    SalaryQuote,
    collapse_whitespace,
)
from trouveur.sources.parse import html_to_text, iso_datetime

version = 1

SOURCE = "jobicy"

_PERIODS = {
    "yearly": SalaryPeriod.YEAR,
    "annual": SalaryPeriod.YEAR,
    "monthly": SalaryPeriod.MONTH,
    "weekly": SalaryPeriod.WEEK,
    "daily": SalaryPeriod.DAY,
    "hourly": SalaryPeriod.HOUR,
}


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    # Every field is `job`-prefixed; there is no bare `title` key.
    title = collapse_whitespace(listing.get("jobTitle"))
    if not listing.get("id") or not title or not external_id:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("url")) or "",
        title=title,
        company=collapse_whitespace(listing.get("companyName")),
        description=html_to_text(listing.get("jobDescription"))
        or collapse_whitespace(listing.get("jobExcerpt")),
        posted_at=iso_datetime(listing.get("pubDate")),
        locations=_locations(listing.get("jobGeo")),
        salary=_salary(listing),
        # Jobicy is a remote-only board.
        remote_hint=True,
        employment_type_hint=_first(listing.get("jobType")),
        department_hint=_first(listing.get("jobIndustry")),
    )


def _locations(value: Any) -> list[Location]:
    raw = collapse_whitespace(value if isinstance(value, str) else None)
    return [Location(raw=raw)] if raw else []


def _salary(listing: dict) -> SalaryQuote | None:
    low, high = _decimal(listing.get("salaryMin")), _decimal(listing.get("salaryMax"))
    if low is None and high is None:
        return None
    return SalaryQuote(
        amount_min=low,
        amount_max=high,
        currency=collapse_whitespace(listing.get("salaryCurrency")),
        period=_PERIODS.get(
            str(listing.get("salaryPeriod") or "").strip().lower(), SalaryPeriod.UNKNOWN
        ),
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _first(values: Any) -> str | None:
    if not isinstance(values, list):
        return collapse_whitespace(values if isinstance(values, str) else None)
    for value in values:
        text = collapse_whitespace(value if isinstance(value, str) else None)
        if text:
            return text
    return None
