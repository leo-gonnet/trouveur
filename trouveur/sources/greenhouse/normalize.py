"""Greenhouse payload -> CanonicalJob. Pure: no I/O, no clock, no database."""

from __future__ import annotations

import html
from typing import Any

from selectolax.parser import HTMLParser

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import iso_datetime

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
        posted_at=iso_datetime(listing.get("first_published")),
        updated_at=iso_datetime(listing.get("updated_at")),
        closes_at=iso_datetime(listing.get("application_deadline")),
        locations=_locations(listing.get("location")),
        salary=None,
        remote_hint=None,
        language_hint=collapse_whitespace(listing.get("language")),
        department_hint=_department(listing.get("departments")),
    )


def _description(content: Any) -> str | None:
    if not content:
        return None
    # `content` is HTML that has itself been HTML-escaped. Unescape once, THEN strip tags.
    markup = html.unescape(str(content))
    return collapse_whitespace(HTMLParser(markup).text(separator=" "))


def _locations(node: Any) -> list[Location]:
    if not isinstance(node, dict):
        return []
    name = collapse_whitespace(node.get("name"))
    if not name:
        return []
    # Several places arrive as one semicolon-separated string; each is kept verbatim.
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
