"""Rippling payload -> CanonicalJob. Pure: no I/O, no clock, no database.

The listing is thin by design here -- title, url, department, one location -- and everything a
reranker reads arrives only on the detail. The detail is therefore preferred field by field,
with the listing as the fallback, so a posting whose detail fetch has not drained yet still
produces a usable row instead of no row.

`description` is an object keyed by section (`company`, `role`, ...), not a string. Reading it as
one yields a Python dict repr in the description column: text that looks like data, embeds like
noise, and is not obviously wrong to a reader skimming the page.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import html_to_text, iso_datetime

version = 1

SOURCE = "rippling"

# The order sections are joined in, so the same posting always hashes the same way. Dict order
# would be the source's, and a reordered payload would flap content_hash and re-bill every score.
_SECTIONS = ("role", "company", "requirements", "benefits")


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    detail = detail if isinstance(detail, dict) else {}
    title = collapse_whitespace(detail.get("name")) or collapse_whitespace(listing.get("name"))
    if not (listing.get("uuid") or detail.get("uuid")) or not title or not external_id:
        return None

    if detail.get("unlistedFromSearch") is True:
        # Rippling still serves the posting, but it is not published on the board.
        return None

    employment = detail.get("employmentType")
    employment = employment if isinstance(employment, dict) else {}

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(detail.get("url")) or collapse_whitespace(listing.get("url")) or "",
        title=title,
        company=collapse_whitespace(detail.get("companyName")),
        description=_description(detail.get("description")),
        posted_at=iso_datetime(detail.get("createdOn")),
        locations=_locations(detail, listing),
        # `payRangeDetails` is a list whose shape was empty on every posting probed, so nothing is
        # claimed from it rather than guessing at a structure never observed carrying data.
        salary=None,
        remote_hint=None,
        employment_type_hint=collapse_whitespace(employment.get("id"))
        or collapse_whitespace(employment.get("label")),
        department_hint=_department(detail.get("department") or listing.get("department")),
    )


def _description(node: Any) -> str | None:
    """Join the detail's section objects in a fixed order."""
    if isinstance(node, str):
        return html_to_text(node)
    if not isinstance(node, dict):
        return None
    keys = [key for key in _SECTIONS if key in node]
    keys += sorted(key for key in node if key not in _SECTIONS)
    parts = [html_to_text(node.get(key)) for key in keys]
    joined = "\n\n".join(part for part in parts if part)
    return joined or None


def _locations(detail: dict, listing: dict) -> list[Location]:
    names = detail.get("workLocations")
    if not isinstance(names, list) or not names:
        node = listing.get("workLocation")
        label = collapse_whitespace(node.get("label")) if isinstance(node, dict) else None
        names = [label] if label else []
    places = []
    for name in names:
        raw = collapse_whitespace(name if isinstance(name, str) else None)
        if raw:
            places.append(Location(raw=raw))
    return places


def _department(node: Any) -> str | None:
    if isinstance(node, str):
        return collapse_whitespace(node)
    if not isinstance(node, dict):
        return None
    return collapse_whitespace(node.get("name")) or collapse_whitespace(node.get("label"))
