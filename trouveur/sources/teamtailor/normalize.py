"""Teamtailor RSS item -> CanonicalJob. Pure: no I/O, no clock, no database.

`remoteStatus` is "none" when the employer said nothing, which is not "on site"; "temporary" is
remote for now, which says nothing about the job either.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import html_to_text, rfc822_datetime

version = 1

SOURCE = "teamtailor"

_REMOTE = {"fully": True, "onsite": False}


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    title = collapse_whitespace(listing.get("title"))
    if not listing.get("guid") or not title or not external_id:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("link")) or "",
        title=title,
        company=collapse_whitespace(listing.get("company_name")),
        description=html_to_text(listing.get("description")),
        posted_at=rfc822_datetime(listing.get("pubDate")),
        locations=_locations(listing.get("locations")),
        salary=None,
        remote_hint=_REMOTE.get(str(listing.get("remoteStatus") or "").strip().lower()),
        department_hint=collapse_whitespace(_text(listing.get("department")))
        or collapse_whitespace(_text(listing.get("division"))),
    )


def _locations(node: Any) -> list[Location]:
    entries = node.get("location") if isinstance(node, dict) else None
    places = []
    for entry in entries if isinstance(entries, list) else [entries]:
        if not isinstance(entry, dict):
            continue
        city = collapse_whitespace(_text(entry.get("city")))
        country = collapse_whitespace(_text(entry.get("country")))
        raw = ", ".join(part for part in (city, country) if part) or collapse_whitespace(
            _text(entry.get("name"))
        )
        if raw:
            places.append(Location(raw=raw, city=city, country=country))
    return places


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None
