"""Greenhouse payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Two shapes here are easy to get wrong and both fail quietly:

  - `content` is HTML that has itself been HTML-escaped, so the JSON holds `&lt;div&gt;`. Stripping
    tags without unescaping first leaves the entity text in the description and strips nothing,
    which reads as a parser that "works" while feeding markup to the embedder and the reranker.
  - `location.name` is free text, and multiple locations arrive semicolon-separated in one string
    ("Remote, Canada; Remote, US"). It is passed through unparsed; interpreting it is derivation.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from trouveur.models import CanonicalJob, Location, collapse_whitespace

version = 1

SOURCE = "greenhouse"


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    job_id = listing.get("id")
    title = collapse_whitespace(listing.get("title"))
    if not job_id or not title or not external_id:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("absolute_url")) or "",
        title=title,
        company=collapse_whitespace(listing.get("company_name")),
        description=_description(listing.get("content")),
        posted_at=_datetime(listing.get("first_published")),
        updated_at=_datetime(listing.get("updated_at")),
        closes_at=_datetime(listing.get("application_deadline")),
        locations=_locations(listing.get("location")),
        # Greenhouse states no salary and no structured work mode. Both stay unset rather than
        # being guessed out of the location string here; derivation reads the raw text instead.
        salary=None,
        remote_hint=None,
        language_hint=collapse_whitespace(listing.get("language")),
        department_hint=_department(listing.get("departments")),
    )


def _description(content: Any) -> str | None:
    if not content:
        return None
    # Unescape once, then strip tags. Doing it the other way round leaves literal &lt;p&gt; text.
    markup = html.unescape(str(content))
    return collapse_whitespace(HTMLParser(markup).text(separator=" "))


def _locations(node: Any) -> list[Location]:
    if not isinstance(node, dict):
        return []
    name = collapse_whitespace(node.get("name"))
    if not name:
        return []
    # Several places arrive as one semicolon-separated string. Each is kept verbatim; no attempt
    # is made here to say which part is a city and which a country.
    return [Location(raw=part) for chunk in name.split(";") if (part := chunk.strip())]


def _department(nodes: Any) -> str | None:
    if not isinstance(nodes, list):
        return None
    names = [
        collapse_whitespace(node.get("name"))
        for node in nodes
        if isinstance(node, dict) and node.get("name")
    ]
    return " / ".join(name for name in names if name) or None


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
