"""SmartRecruiters payloads -> CanonicalJob. Pure: no I/O, no clock, no database.

The listing carries everything but the description, which is the detail's `jobAd.sections`.
"""

from __future__ import annotations

from typing import Any

from trouveur.models import CanonicalJob, Location, collapse_whitespace
from trouveur.sources.parse import html_to_text, iso_datetime

version = 1

SOURCE = "smartrecruiters"

_SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    job_id = str(listing.get("id") or "").strip()
    title = collapse_whitespace(listing.get("name"))
    if not job_id or not title or not external_id:
        return None
    detail = detail if isinstance(detail, dict) else {}
    company = listing.get("company") if isinstance(listing.get("company"), dict) else {}
    location = listing.get("location") if isinstance(listing.get("location"), dict) else {}

    return CanonicalJob(
        source=SOURCE,
        external_id=external_id,
        url=collapse_whitespace(detail.get("postingUrl")) or _url(company, job_id),
        title=title,
        company=collapse_whitespace(company.get("name")),
        description=_description(detail.get("jobAd")),
        posted_at=iso_datetime(listing.get("releasedDate")),
        locations=_locations(location),
        salary=None,
        remote_hint=_remote(location),
        employment_type_hint=_label(listing.get("typeOfEmployment")),
        language_hint=_label(detail.get("language"), key="code"),
        department_hint=_label(listing.get("department")) or _label(listing.get("function")),
    )


def _url(company: dict, job_id: str) -> str:
    identifier = collapse_whitespace(company.get("identifier"))
    return f"https://jobs.smartrecruiters.com/{identifier}/{job_id}" if identifier else ""


def _description(job_ad: Any) -> str | None:
    sections = job_ad.get("sections") if isinstance(job_ad, dict) else None
    if not isinstance(sections, dict):
        return None
    parts = []
    for key in _SECTIONS:
        section = sections.get(key)
        if not isinstance(section, dict):
            continue
        body = html_to_text(section.get("text"))
        if not body:
            continue
        heading = collapse_whitespace(section.get("title"))
        parts.append(f"{heading}\n{body}" if heading else body)
    return "\n\n".join(parts) or None


def _locations(location: dict) -> list[Location]:
    city = collapse_whitespace(location.get("city"))
    region = collapse_whitespace(location.get("region"))
    country = collapse_whitespace(location.get("country"))
    raw = collapse_whitespace(location.get("fullLocation")) or ", ".join(
        part for part in (city, region, country) if part
    )
    return [Location(raw=raw, city=city, region=region, country=country)] if raw else []


def _remote(location: dict) -> bool | None:
    """`remote` is false on a hybrid posting too, and False would make it on-site."""
    if location.get("remote") is True:
        return True
    if location.get("hybrid") is True:
        return None
    if location.get("remote") is False:
        return False
    return None


def _label(node: Any, *, key: str = "label") -> str | None:
    return collapse_whitespace(node.get(key)) if isinstance(node, dict) else None
