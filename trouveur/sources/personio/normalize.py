"""Personio payload -> CanonicalJob. Pure: no I/O, no clock, no database.

The feed is the DACH SME workhorse and it is unusually sparse. Three things about it:

  - **it states no URL.** The canonical link is built from the tenant and the id, which is why the
    external id keeps the tenant in it -- see board.scoped_id. Without that, a posting archived
    here could never be linked back to.
  - the description arrives as a list of titled sections (`Aufgaben`, `Profil`, ...), not one
    string. The section names carry meaning a reader wants, so they are kept in the joined text.
  - `office` and `additionalOffices` are the places, and `subcompany` is the hiring entity. A
    board with one office and a board with several decode to the same list shape.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import iso_datetime

version = 1

SOURCE = "personio"


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    job_id = str(listing.get("id") or "").strip()
    title = collapse_whitespace(listing.get("name"))
    if not job_id or not title or not external_id:
        return None

    # The tenant is only recoverable from the external id, because the feed never names it.
    scope, _, _ = external_id.partition(":")
    if not scope:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=_url(scope, job_id),
        title=title,
        company=collapse_whitespace(listing.get("subcompany")),
        description=_description(listing.get("jobDescriptions")),
        posted_at=iso_datetime(listing.get("createdAt")),
        locations=_locations(listing),
        salary=None,
        remote_hint=None,
        employment_type_hint=_hint(listing.get("employmentType"), listing.get("schedule")),
        department_hint=collapse_whitespace(listing.get("department")),
    )


def _url(scope: str, job_id: str) -> str:
    # Imported lazily-by-value rather than from the client, so the pure normaliser stays free of
    # anything that touches the network.
    return f"https://{scope}.jobs.personio.de/job/{job_id}"


def _description(node: Any) -> str | None:
    """Join the titled sections in the order the feed states them."""
    sections = _as_list(node.get("jobDescription") if isinstance(node, dict) else node)
    parts = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = collapse_whitespace(section.get("name"))
        body = collapse_whitespace(section.get("value"))
        if not body:
            continue
        parts.append(f"{heading}\n{body}" if heading else body)
    return "\n\n".join(parts) or None


def _locations(listing: dict) -> list[Location]:
    names = [collapse_whitespace(listing.get("office"))]
    names += [
        collapse_whitespace(office if isinstance(office, str) else None)
        for office in _as_list(listing.get("additionalOffices"))
    ]
    # Personio states the office as a bare place name ("Wien"), with no country anywhere in the
    # feed. It is passed through unparsed; saying which part is a city is derivation.
    return [Location(raw=name) for name in dict.fromkeys(names) if name]


def _hint(employment_type: Any, schedule: Any) -> str | None:
    parts = [collapse_whitespace(employment_type), collapse_whitespace(schedule)]
    return " ".join(part for part in parts if part) or None


def _as_list(value: Any) -> list:
    """A repeated element decodes to a list, a single one to a scalar; callers want a list."""
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]
