"""Himalayas payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Every posting here is remote, so `remote_hint` is stated rather than inferred from prose.

`locationRestrictions` is the important field and the easy one to misread: it names where a
candidate must be, not where an office is. It is kept as the posting's locations because that is
what a location filter should match against -- a role restricted to the United States is not open
to someone in Vienna, and dropping the field would make it look unrestricted.
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
from trouveur.sources.parse import epoch_datetime, html_to_text

version = 1

SOURCE = "himalayas"

_PERIODS = {
    "annual": SalaryPeriod.YEAR,
    "yearly": SalaryPeriod.YEAR,
    "monthly": SalaryPeriod.MONTH,
    "weekly": SalaryPeriod.WEEK,
    "daily": SalaryPeriod.DAY,
    "hourly": SalaryPeriod.HOUR,
}


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    title = collapse_whitespace(listing.get("title"))
    if not listing.get("guid") or not title or not external_id:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("applicationLink"))
        or collapse_whitespace(listing.get("guid"))
        or "",
        title=title,
        company=collapse_whitespace(listing.get("companyName")),
        description=html_to_text(listing.get("description"))
        or collapse_whitespace(listing.get("excerpt")),
        posted_at=epoch_datetime(listing.get("pubDate")),
        closes_at=epoch_datetime(listing.get("expiryDate")),
        locations=_locations(listing.get("locationRestrictions")),
        salary=_salary(listing),
        # Every posting on this board is remote; the board states nothing else.
        remote_hint=True,
        employment_type_hint=collapse_whitespace(listing.get("employmentType")),
    )


def _locations(values: Any) -> list[Location]:
    if not isinstance(values, list):
        return []
    places = []
    for value in values:
        raw = collapse_whitespace(value if isinstance(value, str) else None)
        if raw:
            places.append(Location(raw=raw))
    return places


def _salary(listing: dict) -> SalaryQuote | None:
    low, high = _decimal(listing.get("minSalary")), _decimal(listing.get("maxSalary"))
    if low is None and high is None:
        return None
    return SalaryQuote(
        amount_min=low,
        amount_max=high,
        currency=collapse_whitespace(listing.get("currency")),
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
