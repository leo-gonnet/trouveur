"""Workable payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Workable is the richest of the global feeds: it states the company, an inline HTML description,
and -- unusually -- a properly structured location object rather than free text. That structure is
kept as stated, so derivation never has to re-parse "Vienna, Vienna, Austria" back into parts a
source already separated.

The description is split across three HTML fields. Only joining `description` would drop the
requirements section, which is most of what a reranker needs to judge a fit.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import html_to_text, iso_datetime

version = 1

SOURCE = "workable"

_SECTIONS = ("description", "requirementsSection", "benefitsSection")


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    title = collapse_whitespace(listing.get("title"))
    if not listing.get("id") or not title or not external_id:
        return None
    if str(listing.get("state") or "published").lower() != "published":
        # Draft and archived postings are served by the same endpoint.
        return None

    company = listing.get("company")
    company = company if isinstance(company, dict) else {}

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("url")) or "",
        title=title,
        company=collapse_whitespace(company.get("title")),
        description=_description(listing),
        posted_at=iso_datetime(listing.get("created")),
        updated_at=iso_datetime(listing.get("updated")),
        locations=_locations(listing),
        salary=None,
        remote_hint=_remote(listing.get("workplace")),
        employment_type_hint=collapse_whitespace(listing.get("employmentType")),
        language_hint=collapse_whitespace(listing.get("language")),
        department_hint=collapse_whitespace(listing.get("department")),
    )


def _description(listing: dict) -> str | None:
    parts = [html_to_text(listing.get(section)) for section in _SECTIONS]
    return "\n\n".join(part for part in parts if part) or None


def _locations(listing: dict) -> list[Location]:
    """Prefer the structured object; fall back to the rendered strings.

    `location` describes one place even when `locations` lists several, so both are read: the
    structured one is kept verbatim, and any additional rendered string becomes its own entry.
    """
    places: list[Location] = []
    node = listing.get("location")
    primary_raw = None
    if isinstance(node, dict):
        city = collapse_whitespace(node.get("city"))
        region = collapse_whitespace(node.get("subregion"))
        country = collapse_whitespace(node.get("countryName"))
        primary_raw = ", ".join(part for part in (city, region, country) if part) or None
        if primary_raw:
            places.append(Location(raw=primary_raw, city=city, region=region, country=country))

    for name in listing.get("locations") or []:
        raw = collapse_whitespace(name if isinstance(name, str) else None)
        if raw and raw != primary_raw:
            places.append(Location(raw=raw))
    return places


def _remote(workplace: Any) -> bool | None:
    value = str(workplace or "").strip().lower()
    if value == "remote":
        return True
    if value in {"on_site", "onsite", "hybrid"}:
        return False
    return None
