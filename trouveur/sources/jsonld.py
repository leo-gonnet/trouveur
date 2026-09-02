"""Generic schema.org JobPosting parser.

karriere.at, most ATS boards and most company career pages emit `JobPosting` JSON-LD, so one
parser serves all of them and a new source becomes a URL rather than a new scraper.

Parses defensively: sources change without notice, so a missing optional field is None and an
unparseable field never discards the whole record.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from selectolax.parser import HTMLParser

from trouveur.models import Job, SalaryPeriod

log = logging.getLogger(__name__)

_PERIOD = {
    "YEAR": SalaryPeriod.YEAR,
    "MONTH": SalaryPeriod.MONTH,
    "WEEK": SalaryPeriod.WEEK,
    "DAY": SalaryPeriod.DAY,
    "HOUR": SalaryPeriod.HOUR,
}
_WS = re.compile(r"\s+")


def extract_blocks(html: str) -> list[dict]:
    """Return every JSON-LD object in the document, flattening arrays and @graph."""
    blocks: list[dict] = []
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        raw = node.text(strip=False)
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("skipping malformed JSON-LD block")
            continue
        blocks.extend(_flatten(payload))
    return blocks


def _flatten(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        out: list[dict] = []
        for entry in payload:
            out.extend(_flatten(entry))
        return out
    if isinstance(payload, dict):
        if "@graph" in payload:
            return _flatten(payload["@graph"])
        return [payload]
    return []


def find_job_posting(html: str) -> dict | None:
    for block in extract_blocks(html):
        types = block.get("@type")
        types = types if isinstance(types, list) else [types]
        if "JobPosting" in types:
            return block
    return None


def to_job(posting: dict, *, source: str, url: str, native_id: str | None = None) -> Job | None:
    title = _text(posting.get("title"))
    if not title:
        return None

    org = posting.get("hiringOrganization") or {}
    company = _text(org.get("name")) if isinstance(org, dict) else _text(org)

    city, country = _location(posting.get("jobLocation"))
    salary_min, salary_max, period = _salary(posting.get("baseSalary"))

    remote = None
    if posting.get("jobLocationType") == "TELECOMMUTE":
        remote = True
    elif posting.get("applicantLocationRequirements") and not city:
        remote = True

    identifier = native_id or _identifier(posting) or url

    return Job(
        source=source,
        source_native_id=str(identifier),
        url=url,
        title=title,
        company=company or None,
        location_city=city,
        location_country=country,
        remote=remote,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_period=period,
        posted_at=_datetime(posting.get("datePosted")),
        description=_html_to_text(posting.get("description")),
        raw={"jsonld": posting},
    )


def _identifier(posting: dict) -> str | None:
    ident = posting.get("identifier")
    if isinstance(ident, dict):
        return _text(ident.get("value")) or _text(ident.get("name"))
    return _text(ident)


def _location(node: Any) -> tuple[str | None, str | None]:
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return None, None
    address = node.get("address")
    if isinstance(address, list):
        address = address[0] if address else None
    if not isinstance(address, dict):
        return None, None
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    return _text(address.get("addressLocality")), _text(country)


def _salary(node: Any) -> tuple[Decimal | None, Decimal | None, SalaryPeriod]:
    if not isinstance(node, dict):
        return None, None, SalaryPeriod.UNKNOWN
    value = node.get("value")
    if not isinstance(value, dict):
        return _decimal(value), None, SalaryPeriod.UNKNOWN
    period = _PERIOD.get(str(value.get("unitText") or "").upper(), SalaryPeriod.UNKNOWN)
    exact = _decimal(value.get("value"))
    low = _decimal(value.get("minValue")) or exact
    high = _decimal(value.get("maxValue")) or exact
    return low, high, period


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = _WS.sub(" ", str(value)).strip()
    return cleaned or None


def _html_to_text(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value)
    text = HTMLParser(raw).text(separator=" ") if "<" in raw else raw
    return _WS.sub(" ", text).strip() or None
