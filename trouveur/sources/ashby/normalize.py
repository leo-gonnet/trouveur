"""Ashby payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Ashby is unusually generous: it states the description as plain text, the address as structured
fields, and -- with includeCompensation -- salary as numbers rather than a rendered string. All
three are taken as stated rather than re-derived from prose.

Two shapes are easy to get wrong:

  - `title` arrives with leading whitespace on real boards (" Security Engineer, Cloud"), so it
    must be collapsed before it becomes an identity input.
  - `secondaryLocations` holds the other places a posting names, and dropping it loses every
    remote-in-another-country variant of the same role.
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
from trouveur.sources.parse import iso_datetime

version = 1

SOURCE = "ashby"

# Ashby's own interval vocabulary. Anything absent stays UNKNOWN rather than being guessed:
# annualising an interval we misread would silently distort every salary filter.
_PERIODS = {
    "YEAR": SalaryPeriod.YEAR,
    "ANNUAL": SalaryPeriod.YEAR,
    "MONTH": SalaryPeriod.MONTH,
    "MONTHLY": SalaryPeriod.MONTH,
    "WEEK": SalaryPeriod.WEEK,
    "WEEKLY": SalaryPeriod.WEEK,
    "DAY": SalaryPeriod.DAY,
    "DAILY": SalaryPeriod.DAY,
    "HOUR": SalaryPeriod.HOUR,
    "HOURLY": SalaryPeriod.HOUR,
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
        url=collapse_whitespace(listing.get("jobUrl"))
        or collapse_whitespace(listing.get("applyUrl"))
        or "",
        title=title,
        company=None,
        description=collapse_whitespace(listing.get("descriptionPlain")),
        posted_at=iso_datetime(listing.get("publishedAt")),
        updated_at=iso_datetime(listing.get("updatedAt")),
        locations=_locations(listing),
        salary=_salary(listing.get("compensation")),
        remote_hint=listing.get("isRemote") if isinstance(listing.get("isRemote"), bool) else None,
        employment_type_hint=collapse_whitespace(listing.get("employmentType")),
        department_hint=_department(listing),
    )


def _locations(listing: dict) -> list[Location]:
    places: list[Location] = []
    primary = collapse_whitespace(listing.get("location"))
    if primary:
        places.append(_with_address(primary, listing.get("address")))

    for node in listing.get("secondaryLocations") or []:
        if not isinstance(node, dict):
            continue
        name = collapse_whitespace(node.get("location"))
        if name:
            places.append(_with_address(name, node.get("address")))
    return places


def _with_address(raw: str, address: Any) -> Location:
    """Keep the source's own structured address where it stated one.

    Derivation parses `raw` only where structure is absent, so a source that already knows the
    answer is never second-guessed.
    """
    postal = (address or {}).get("postalAddress") if isinstance(address, dict) else None
    if not isinstance(postal, dict):
        return Location(raw=raw)
    return Location(
        raw=raw,
        city=collapse_whitespace(postal.get("addressLocality")),
        region=collapse_whitespace(postal.get("addressRegion")),
        country=collapse_whitespace(postal.get("addressCountry")),
    )


def _salary(compensation: Any) -> SalaryQuote | None:
    if not isinstance(compensation, dict):
        return None
    for component in compensation.get("summaryComponents") or []:
        if not isinstance(component, dict):
            continue
        # Equity and bonus components share this shape; only base pay is a salary.
        if str(component.get("compensationType") or "").upper() not in {"", "SALARY"}:
            continue
        low, high = _decimal(component.get("minValue")), _decimal(component.get("maxValue"))
        if low is None and high is None:
            continue
        return SalaryQuote(
            amount_min=low,
            amount_max=high,
            currency=collapse_whitespace(component.get("currencyCode")),
            period=_PERIODS.get(
                str(component.get("interval") or "").upper(), SalaryPeriod.UNKNOWN
            ),
        )
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _department(listing: dict) -> str | None:
    parts = [
        collapse_whitespace(listing.get("department")),
        collapse_whitespace(listing.get("team")),
    ]
    named = [part for part in parts if part]
    return " / ".join(dict.fromkeys(named)) or None
