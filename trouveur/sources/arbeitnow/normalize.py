"""Arbeitnow payload -> CanonicalJob. Pure: no I/O, no clock, no database.

`created_at` is epoch SECONDS, where Lever states milliseconds; both go through the same helper.
"""

from __future__ import annotations

import re
from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import epoch_datetime, html_to_text

version = 2

SOURCE = "arbeitnow"

# About one posting in seven, copied from an applicant tracker, carries its HTML escaped inside
# real HTML: one pass of parsing leaves "<p>" in the text.
_ESCAPED_MARKUP = re.compile(r"&lt;/?[a-z]", re.IGNORECASE)
# Every description ends "Find (more English Speaking) Jobs in <country> on Arbeitnow", naming
# the market the posting is filed under. It is the only place a posting copied from a tracker
# with a blank `location` says where it is, and it is a market: Vienna postings say Germany.
_MARKET = re.compile(r"Jobs in ([^\W\d_][^.]*?) on Arbeitnow\s*$")


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    title = collapse_whitespace(listing.get("title"))
    if not listing.get("slug") or not title or not external_id:
        return None

    description = _description(listing.get("description"))
    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(listing.get("url")) or "",
        title=title,
        company=collapse_whitespace(listing.get("company_name")),
        description=description,
        posted_at=epoch_datetime(listing.get("created_at")),
        locations=_locations(listing.get("location"), description),
        salary=None,
        remote_hint=listing.get("remote") if isinstance(listing.get("remote"), bool) else None,
        employment_type_hint=_first(listing.get("job_types")),
    )


def _description(value: Any) -> str | None:
    text = html_to_text(value)
    if text and _ESCAPED_MARKUP.search(str(value)):
        return html_to_text(text)
    return text


def _locations(value: Any, description: str | None) -> list[Location]:
    raw = collapse_whitespace(value if isinstance(value, str) else None) or ""
    market = _MARKET.search(description or "")
    hint = market.group(1) if market else None
    return [Location(raw=raw, country_hint=hint)] if raw or hint else []


def _first(values: Any) -> str | None:
    if not isinstance(values, list):
        return None
    for value in values:
        text = collapse_whitespace(value if isinstance(value, str) else None)
        if text:
            return text
    return None
