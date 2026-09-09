"""Arbeitnow payload -> CanonicalJob. Pure: no I/O, no clock, no database.

`created_at` is epoch **seconds** here, where Lever states milliseconds. Both decode through the
same helper so neither can be read as the other -- a posting placed in 1970 would be dropped by
every recency filter and by the delta window that decides when a sweep stops.

`job_types` and `tags` are the source's own free-form labels. They are passed through as a hint,
not mapped to an employment type here; mapping vocabulary is derivation.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import epoch_datetime, html_to_text

version = 1

SOURCE = "arbeitnow"


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    title = collapse_whitespace(listing.get("title"))
    if not listing.get("slug") or not title or not external_id:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("url")) or "",
        title=title,
        company=collapse_whitespace(listing.get("company_name")),
        description=html_to_text(listing.get("description")),
        posted_at=epoch_datetime(listing.get("created_at")),
        locations=_locations(listing.get("location")),
        salary=None,
        remote_hint=listing.get("remote") if isinstance(listing.get("remote"), bool) else None,
        employment_type_hint=_first(listing.get("job_types")),
    )


def _locations(value: Any) -> list[Location]:
    raw = collapse_whitespace(value if isinstance(value, str) else None)
    # A bare place name ("Dresden"), passed through unparsed.
    return [Location(raw=raw)] if raw else []


def _first(values: Any) -> str | None:
    if not isinstance(values, list):
        return None
    for value in values:
        text = collapse_whitespace(value if isinstance(value, str) else None)
        if text:
            return text
    return None
