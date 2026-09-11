"""Breezy HR payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Breezy names the title `name` and states the company on every posting, which most per-tenant
boards do not. Its `salary` is a rendered string ("$0.05 - $0.06 / hour") rather than numbers;
it is deliberately not parsed here, because a regex over a formatted range is exactly the kind of
guess that produces a confidently wrong figure. A wrong salary is worse than no salary.
"""

from __future__ import annotations

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import html_to_text, iso_datetime

version = 1

SOURCE = "breezy"


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    # Breezy names the title `name`.
    title = collapse_whitespace(listing.get("name"))
    if not listing.get("id") or not title or not external_id:
        return None

    company = listing.get("company")
    company = company if isinstance(company, dict) else {}
    employment = listing.get("type")
    employment = employment if isinstance(employment, dict) else {}

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("url")) or "",
        title=title,
        company=collapse_whitespace(company.get("name")),
        description=html_to_text(listing.get("description")),
        posted_at=iso_datetime(listing.get("published_date")),
        updated_at=iso_datetime(listing.get("updated_date")),
        locations=_locations(listing),
        # `salary` is a formatted string, not numbers. Left unset rather than parsed out of prose.
        salary=None,
        remote_hint=_remote(listing),
        employment_type_hint=collapse_whitespace(employment.get("name")),
        department_hint=collapse_whitespace(listing.get("department")),
    )


def _locations(listing: dict) -> list[Location]:
    nodes = listing.get("locations")
    if not isinstance(nodes, list) or not nodes:
        single = listing.get("location")
        nodes = [single] if isinstance(single, dict) else []

    places: list[Location] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        place = _place(node)
        if place:
            places.append(place)
    return places


def _place(node: dict) -> Location | None:
    """Keep Breezy's structured city and country where it states them.

    `country` and `state` are objects with `name`, not strings; reading them as strings yields a
    dict repr in the country column, which then matches no vocabulary entry at all.
    """
    raw = collapse_whitespace(node.get("name"))
    city = collapse_whitespace(node.get("city"))
    country = node.get("country")
    region = node.get("state")
    country_name = (
        collapse_whitespace(country.get("name")) if isinstance(country, dict) else None
    )
    region_name = collapse_whitespace(region.get("name")) if isinstance(region, dict) else None

    raw = raw or ", ".join(part for part in (city, region_name, country_name) if part)
    if not raw:
        return None
    return Location(raw=raw, city=city, region=region_name, country=country_name)


def _remote(listing: dict) -> bool | None:
    for key in ("location", "locations"):
        node = listing.get(key)
        nodes = node if isinstance(node, list) else [node]
        for entry in nodes:
            if isinstance(entry, dict) and isinstance(entry.get("is_remote"), bool):
                return entry["is_remote"]
    return None
