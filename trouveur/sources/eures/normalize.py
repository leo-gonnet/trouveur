"""EURES payload -> CanonicalJob. Pure: no I/O, no clock, no database.

A posting is placed by NUTS code alone ({"DE": ["DE300"]}), never by town, and often by country
alone ({"EL": [null]}). The code's own name is written as the place, so a region that is a city
("Berlin", "Madrid") can be read as one, and any other still names its country.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.eures import nuts
from trouveur.sources.parse import epoch_datetime, html_to_text

version = 1

SOURCE = "eures"

_DETAILS_URL = "https://europa.eu/eures/portal/jv-se/jv-details/{id}?lang=en"

# What national services write where the employer withheld its name. Kept as a company, every
# such posting would read as one employer's, which dedup and the reranker both take at its word.
_NO_EMPLOYER = frozenset({"non renseigné", "siehe beschreibung"})


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    job_id = str(listing.get("id") or "").strip()
    title = collapse_whitespace(listing.get("title"))
    if not job_id or not title or not external_id:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=_DETAILS_URL.format(id=job_id),
        title=title,
        company=_company(listing.get("employer")),
        description=html_to_text(listing.get("description")),
        posted_at=epoch_datetime(listing.get("creationDate")),
        updated_at=epoch_datetime(listing.get("lastModificationDate")),
        locations=_locations(listing.get("locationMap")),
        salary=None,
        remote_hint=None,
        employment_type_hint=_employment(listing),
        language_hint=_first(listing.get("availableLanguages")),
    )


def _company(employer: Any) -> str | None:
    name = collapse_whitespace(employer.get("name")) if isinstance(employer, dict) else None
    if not name or name.casefold() in _NO_EMPLOYER:
        return None
    return name


def _locations(location_map: Any) -> list[Location]:
    if not isinstance(location_map, dict):
        return []
    places: list[Location] = []
    for country_code, regions in location_map.items():
        country = nuts.label(str(country_code)) or collapse_whitespace(str(country_code))
        regions = regions if isinstance(regions, list) else []
        codes = [code for code in regions if isinstance(code, str)]
        if not codes:
            places.append(Location(raw=country or "", country=country))
            continue
        for code in dict.fromkeys(codes):
            region = nuts.label(code)
            raw = ", ".join(part for part in (region, country) if part)
            places.append(Location(raw=raw or code, region=region, country=country))
    return places


def _employment(listing: dict) -> str | None:
    schedule = listing.get("positionScheduleCodes")
    parts = [collapse_whitespace(listing.get("positionOfferingCode"))]
    if isinstance(schedule, list):
        parts += [collapse_whitespace(code) for code in schedule if isinstance(code, str)]
    return " ".join(part for part in parts if part) or None


def _first(values: Any) -> str | None:
    if not isinstance(values, list):
        return None
    return next((value for value in values if isinstance(value, str) and value), None)
