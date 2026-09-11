"""Lever payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Three shapes are easy to get wrong and each fails quietly:

  - **the title is `text`, not `title`.** Lever has no `title` key at all, so reading one yields
    None and the posting is dropped as untitled -- a whole board silently normalising to nothing.
  - `createdAt` is epoch **milliseconds**. Read as seconds it lands in 1970 and every recency
    filter and delta window treats a new posting as ancient.
  - `categories.allLocations` holds every place the role is offered; `categories.location` is
    only the first. Using the singular loses the rest.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import epoch_datetime

version = 1

SOURCE = "lever"


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    # Lever names the title `text`. There is no `title` key on this payload.
    title = collapse_whitespace(listing.get("text"))
    if not listing.get("id") or not title or not external_id:
        return None

    categories = listing.get("categories")
    categories = categories if isinstance(categories, dict) else {}

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("hostedUrl"))
        or collapse_whitespace(listing.get("applyUrl"))
        or "",
        title=title,
        company=None,
        description=_description(listing),
        posted_at=epoch_datetime(listing.get("createdAt")),
        locations=_locations(categories, listing.get("country")),
        salary=None,
        remote_hint=_remote(listing.get("workplaceType")),
        employment_type_hint=collapse_whitespace(categories.get("commitment")),
        department_hint=collapse_whitespace(categories.get("team")),
    )


def _description(listing: dict) -> str | None:
    """Lever splits the posting across several plain-text fields; all of them are the posting.

    `descriptionPlain` is the opening section only. Dropping the body would hand the reranker an
    intro paragraph and nothing about the actual role.
    """
    parts = [
        collapse_whitespace(listing.get("descriptionPlain")),
        collapse_whitespace(listing.get("descriptionBodyPlain")),
        collapse_whitespace(listing.get("additionalPlain")),
    ]
    joined = "\n\n".join(part for part in parts if part)
    return joined or None


def _locations(categories: dict, country: Any) -> list[Location]:
    names = categories.get("allLocations")
    if not isinstance(names, list) or not names:
        single = collapse_whitespace(categories.get("location"))
        names = [single] if single else []

    country_code = collapse_whitespace(country)
    places: list[Location] = []
    for name in names:
        raw = collapse_whitespace(name)
        if raw:
            # `country` is a single code for the whole posting, so it only describes a location
            # list of one. Attaching it to each of several would assert something Lever did not.
            places.append(Location(raw=raw, country=country_code if len(names) == 1 else None))
    return places


def _remote(workplace_type: Any) -> bool | None:
    value = str(workplace_type or "").strip().lower()
    if value == "remote":
        return True
    if value in {"onsite", "on-site", "hybrid"}:
        return False
    return None
