"""Workday payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Two listing fields are traps and neither is read: `postedOn` is relative prose ("Posted Today"),
and `locationsText` is a count ("3 Locations"), not a place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import html_to_text

version = 1

SOURCE = "workday"


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    detail = detail if isinstance(detail, dict) else {}
    info = detail.get("jobPostingInfo")
    info = info if isinstance(info, dict) else {}

    title = collapse_whitespace(info.get("title")) or collapse_whitespace(listing.get("title"))
    if not external_id or not title:
        return None
    if info and info.get("posted") is False:
        # Still addressable, but no longer published on the board.
        return None

    scope, _, path = external_id.partition(":")
    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(info.get("externalUrl")) or _url(scope, path),
        title=title,
        company=_company(detail),
        description=html_to_text(info.get("jobDescription")),
        # `startDate` is the only absolute date either payload states. `postedOn` is prose.
        posted_at=_date(info.get("startDate")),
        locations=_locations(info),
        salary=None,
        remote_hint=None,
        employment_type_hint=collapse_whitespace(info.get("timeType")),
    )


def _url(scope: str, path: str) -> str:
    """Rebuild the public posting URL from the scope, for a row whose detail has not drained."""
    parts = scope.split(":", 2)
    if len(parts) != 3 or not path:
        return ""
    tenant, instance, site = parts
    return f"https://{tenant}.{instance}.myworkdayjobs.com/{site}{path}"


def _company(detail: dict) -> str | None:
    organisation = detail.get("hiringOrganization")
    if not isinstance(organisation, dict):
        return None
    return collapse_whitespace(organisation.get("name"))


def _locations(info: dict) -> list[Location]:
    """The detail's real location strings. `locationsText` from the listing is never one."""
    names = [collapse_whitespace(info.get("location"))]
    for extra in info.get("additionalLocations") or []:
        names.append(collapse_whitespace(extra if isinstance(extra, str) else None))

    country = info.get("country")
    country_name = (
        collapse_whitespace(country.get("descriptor")) if isinstance(country, dict) else None
    )
    places = []
    for name in dict.fromkeys(name for name in names if name):
        # One code for the whole posting, so it only describes a location list of one.
        places.append(Location(raw=name, country=country_name if len(names) == 1 else None))
    return places


def _date(value: Any) -> datetime | None:
    """`startDate` is a bare calendar date; it is read as UTC midnight, never as local time."""
    text = collapse_whitespace(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
