"""Recruitee payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Dates are written "2026-10-26 19:43:14 UTC", a suffix `fromisoformat` rejects, so a posting would
silently lose its date without `_date`.
"""

from __future__ import annotations

from datetime import datetime
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

SOURCE = "recruitee"

_PERIODS = {
    "year": SalaryPeriod.YEAR,
    "month": SalaryPeriod.MONTH,
    "week": SalaryPeriod.WEEK,
    "day": SalaryPeriod.DAY,
    "hour": SalaryPeriod.HOUR,
}


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    title = collapse_whitespace(listing.get("title"))
    if not listing.get("id") or not title or not external_id:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("careers_url")) or "",
        title=title,
        company=collapse_whitespace(listing.get("company_name")),
        description=_description(listing),
        posted_at=_date(listing.get("published_at")) or _date(listing.get("created_at")),
        updated_at=_date(listing.get("updated_at")),
        closes_at=_date(listing.get("close_at")),
        locations=_locations(listing),
        salary=_salary(listing.get("salary")),
        remote_hint=_remote(listing),
        employment_type_hint=collapse_whitespace(listing.get("employment_type_code")),
        department_hint=collapse_whitespace(listing.get("department")),
    )


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    return iso_datetime(value.strip().removesuffix(" UTC"))


def _description(listing: dict) -> str | None:
    parts = [html_to_text(listing.get(key)) for key in ("description", "requirements")]
    return "\n\n".join(part for part in parts if part) or None


def _locations(listing: dict) -> list[Location]:
    places = []
    for node in listing.get("locations") or []:
        if not isinstance(node, dict):
            continue
        city = collapse_whitespace(node.get("city"))
        region = collapse_whitespace(node.get("state"))
        country = collapse_whitespace(node.get("country"))
        raw = ", ".join(part for part in (city, region, country) if part)
        if raw:
            places.append(Location(raw=raw, city=city, region=region, country=country))
    if places:
        return places
    raw = collapse_whitespace(listing.get("location"))
    return [Location(raw=raw)] if raw else []


def _remote(listing: dict) -> bool | None:
    if listing.get("remote") is True:
        return True
    if listing.get("hybrid") is True:
        return None
    if listing.get("on_site") is True:
        return False
    return None


def _salary(node: Any) -> SalaryQuote | None:
    if not isinstance(node, dict):
        return None
    low, high = _decimal(node.get("min")), _decimal(node.get("max"))
    if low is None and high is None:
        return None
    return SalaryQuote(
        amount_min=low,
        amount_max=high,
        currency=collapse_whitespace(node.get("currency")),
        period=_PERIODS.get(str(node.get("period") or "").strip().lower(), SalaryPeriod.UNKNOWN),
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
