"""Personio public XML feeds — DACH Mittelstand.

`https://{tenant}.jobs.personio.de/xml` is public and needs no authentication. There is no global
index of tenants, so this source only sees the ones stored in `personio_tenant` and edited in the
web UI. Some tenants 307-redirect and some are retired, so non-200 responses are skipped rather
than treated as failures.

The feed carries no salary and no country — only an office city — so those stay None.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from trouveur.models import Job
from trouveur.sources.http import PoliteClient

log = logging.getLogger(__name__)

_WS = " "

# A tenant is the subdomain in {tenant}.jobs.personio.de, so it is a DNS label: lowercase
# alphanumerics and hyphens. The CHECK constraint in migration 0003 enforces the same shape.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HOST = re.compile(r"^(?:https?://)?([a-z0-9][a-z0-9-]*)\.jobs\.personio\.(?:de|com)\b")


def parse_tenant(value: str) -> str | None:
    """Extract a tenant slug from a slug or a careers-page URL. None if it is neither.

    Bulk paste is the main way this list gets filled, and what people paste is whatever their
    browser showed them, so a full URL has to work as well as a bare slug.
    """
    candidate = (value or "").strip().lower().rstrip("/")
    if not candidate:
        return None
    host = _HOST.match(candidate)
    if host:
        return host.group(1)
    # Reject anything else URL-shaped rather than storing a slug we invented from a stray path.
    if "/" in candidate or "." in candidate or ":" in candidate:
        return None
    return candidate if _SLUG.match(candidate) else None


def parse_tenants(raw: str) -> tuple[list[str], list[str]]:
    """Split a pasted block into (recognised slugs, unrecognised entries), preserving order.

    Separators are newlines, commas and semicolons -- deliberately not spaces. A slug can never
    contain one, so splitting on spaces would turn a mistyped line into several plausible-looking
    tenants instead of one visible rejection.
    """
    seen: set[str] = set()
    good: list[str] = []
    bad: list[str] = []
    for token in re.split(r"[\n\r,;]+", raw or ""):
        if not token.strip():
            continue
        slug = parse_tenant(token)
        if slug is None:
            bad.append(token.strip())
        elif slug not in seen:
            seen.add(slug)
            good.append(slug)
    return good, bad


class PersonioSource:
    name = "personio"

    def __init__(self, tenants: list[str]) -> None:
        self.tenants = tenants

    async def fetch(self, client: PoliteClient, since: datetime | None) -> AsyncIterator[Job]:
        for tenant in self.tenants:
            try:
                jobs = await self._tenant(client, tenant)
            except Exception as exc:  # noqa: BLE001 - one dead tenant must not stop the rest
                log.warning("personio: tenant %r failed: %s", tenant, exc)
                continue
            log.debug("personio: %s yielded %d jobs", tenant, len(jobs))
            for job in jobs:
                yield job

    async def _tenant(self, client: PoliteClient, tenant: str) -> list[Job]:
        url = f"https://{tenant}.jobs.personio.de/xml"
        response = await client.get(url)
        if response.status_code != 200:
            log.debug("personio: %s returned %s, skipping", tenant, response.status_code)
            return []

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            log.warning("personio: %s returned unparseable XML: %s", tenant, exc)
            return []

        jobs = []
        for position in root.iter("position"):
            job = _to_job(position, tenant)
            if job is not None:
                jobs.append(job)
        return jobs


def _to_job(position: ET.Element, tenant: str) -> Job | None:
    job_id = _text(position.findtext("id"))
    title = _text(position.findtext("name"))
    if not job_id or not title:
        return None

    offices = [
        _text(position.findtext("office")),
        _text(position.findtext("additionalOffices")),
    ]
    city = next((office for office in offices if office), None)

    return Job(
        source="personio",
        source_native_id=f"{tenant}:{job_id}",
        url=f"https://{tenant}.jobs.personio.de/job/{job_id}",
        title=title,
        company=_text(position.findtext("subcompany")) or tenant,
        location_city=city,
        # The feed states no country; guessing from an office name would be worse than None.
        location_country=None,
        remote=None,
        posted_at=_datetime(position.findtext("createdAt")),
        description=_description(position),
        raw={
            "tenant": tenant,
            "department": _text(position.findtext("department")),
            "employmentType": _text(position.findtext("employmentType")),
            "seniority": _text(position.findtext("seniority")),
            "schedule": _text(position.findtext("schedule")),
            "yearsOfExperience": _text(position.findtext("yearsOfExperience")),
        },
    )


def _description(position: ET.Element) -> str | None:
    """Join the feed's description sections. Some tenants publish none at all."""
    parts = []
    for section in position.iter("jobDescription"):
        name = _text(section.findtext("name"))
        value = _text(section.findtext("value"))
        if value:
            parts.append(f"{name}: {value}" if name else value)
    return _WS.join(parts) or None


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
