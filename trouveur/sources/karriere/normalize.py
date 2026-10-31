"""karriere.at payloads -> CanonicalJob. Pure: no I/O, no clock, no database.

The listing is a search row: title, employer, town names and a summary. The detail is the posting
page's schema.org JobPosting, which adds the description, the date and a structured address.
The board is Austrian, so Austria is only a hint on a bare town name: an address states its own.
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

SOURCE = "karriere"

_POSTING_URL = "https://www.karriere.at/jobs/{id}"

_PERIODS = {
    "YEAR": SalaryPeriod.YEAR,
    "MONTH": SalaryPeriod.MONTH,
    "WEEK": SalaryPeriod.WEEK,
    "DAY": SalaryPeriod.DAY,
    "HOUR": SalaryPeriod.HOUR,
}


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    job_id = str(listing.get("id") or "").strip()
    detail = detail if isinstance(detail, dict) else {}
    title = collapse_whitespace(detail.get("title")) or collapse_whitespace(listing.get("title"))
    if not job_id or not title or not external_id:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("link")) or _POSTING_URL.format(id=job_id),
        title=title,
        company=_company(listing, detail),
        description=html_to_text(detail.get("description"))
        or collapse_whitespace(listing.get("summary")),
        posted_at=iso_datetime(detail.get("datePosted")),
        closes_at=iso_datetime(detail.get("validThrough")),
        locations=_locations(listing, detail),
        salary=_salary(detail.get("baseSalary")),
        remote_hint=True if detail.get("jobLocationType") == "TELECOMMUTE" else None,
        employment_type_hint=collapse_whitespace(listing.get("employmentTypes"))
        or _joined(detail.get("employmentType")),
    )


def _company(listing: dict, detail: dict) -> str | None:
    company = listing.get("company")
    if isinstance(company, dict):
        name = collapse_whitespace(company.get("name"))
        if name:
            return name
    organisation = detail.get("hiringOrganization")
    return collapse_whitespace(organisation.get("name")) if isinstance(organisation, dict) else None


def _locations(listing: dict, detail: dict) -> list[Location]:
    places = []
    for node in _as_list(detail.get("jobLocation")):
        address = node.get("address") if isinstance(node, dict) else None
        if not isinstance(address, dict):
            continue
        city = collapse_whitespace(address.get("addressLocality"))
        region = collapse_whitespace(address.get("addressRegion"))
        country = collapse_whitespace(address.get("addressCountry"))
        raw = ", ".join(part for part in (city, region, country) if part)
        if raw:
            places.append(Location(raw=raw, city=city, region=region, country=country))
    if places:
        return places
    names = [
        collapse_whitespace(node.get("name"))
        for node in _as_list(listing.get("locations"))
        if isinstance(node, dict)
    ]
    return [Location(raw=name, country_hint="Austria") for name in dict.fromkeys(names) if name]


def _salary(node: Any) -> SalaryQuote | None:
    if not isinstance(node, dict):
        return None
    value = node.get("value")
    if isinstance(value, dict):
        low = _decimal(value.get("minValue")) or _decimal(value.get("value"))
        high = _decimal(value.get("maxValue"))
        unit = str(value.get("unitText") or "").upper()
    else:
        low, high, unit = _decimal(value), None, ""
    if low is None and high is None:
        return None
    return SalaryQuote(
        amount_min=low,
        amount_max=high,
        currency=collapse_whitespace(node.get("currency")),
        period=_PERIODS.get(unit, SalaryPeriod.UNKNOWN),
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _joined(values: Any) -> str | None:
    parts = [collapse_whitespace(value) for value in _as_list(values) if isinstance(value, str)]
    return " ".join(part for part in parts if part) or None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
