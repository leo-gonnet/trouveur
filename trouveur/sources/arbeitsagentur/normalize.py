"""Arbeitsagentur payload -> CanonicalJob. Pure: no I/O, no clock, no database.

Purity is the whole point. This function is replayed over the archive whenever it is fixed, so
anything it reads that is not its arguments would make the replay disagree with the original run.

It produces structure only. Mapping DEUTSCHLAND to DE, annualising a monthly salary and deciding
whether "Ingenieur (m/w/d)" is a senior role are all interpretation, and all belong in derivation
where they can be re-run without re-fetching a million postings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from trouveur.models import CanonicalJob, Location, SalaryPeriod, SalaryQuote, collapse_whitespace

version = 1

SOURCE = "arbeitsagentur"

_PERIOD = {
    "JAHRESGEHALT": SalaryPeriod.YEAR,
    "MONATSGEHALT": SalaryPeriod.MONTH,
    "STUNDENLOHN": SalaryPeriod.HOUR,
}


def normalize(
    listing: dict, detail: dict | None = None, *, external_id: str | None = None
) -> CanonicalJob | None:
    """Merge a listing with its optional detail payload.

    The listing is the floor and the detail only ever adds: a detail fetch that failed arrives
    here as None, and a posting that has since been retired 404s and also arrives as None. Neither
    may blank a description we already hold, which is why the two payloads are archived separately
    rather than one overwriting the other.
    """
    merged: dict[str, Any] = {**listing, **(detail or {})}

    external_id = external_id or merged.get("referenznummer")
    title = collapse_whitespace(
        merged.get("stellenangebotsTitel") or merged.get("hauptberuf")
    )
    if not external_id or not title:
        return None

    return CanonicalJob(
        source=SOURCE,
        external_id=str(external_id),
        url=_url(merged, str(external_id)),
        title=title,
        company=collapse_whitespace(merged.get("firma")),
        description=collapse_whitespace(merged.get("stellenangebotsBeschreibung")),
        posted_at=_date(merged.get("datumErsteVeroeffentlichung")),
        updated_at=_date(merged.get("aenderungsdatum")),
        locations=_locations(merged.get("stellenlokationen")),
        salary=_salary(merged),
        remote_hint=_bool(merged.get("homeofficemoeglich")),
        employment_type_hint=_employment_hint(merged),
        agency_hint=_agency_hint(detail),
        language_hint="de",
        department_hint=collapse_whitespace(merged.get("hauptberuf")),
    )


def _url(item: dict, external_id: str) -> str:
    """Prefer the employer's own advert; fall back to the Arbeitsagentur page.

    externeURL is where the vacancy actually lives and is the useful destination for an applicant,
    but it is absent on postings that exist only inside the Jobbörse.
    """
    external = collapse_whitespace(item.get("externeURL"))
    if external:
        return external
    return f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{external_id}"


def _locations(nodes: Any) -> list[Location]:
    if not isinstance(nodes, list):
        return []
    locations = []
    for node in nodes:
        address = (node or {}).get("adresse") if isinstance(node, dict) else None
        if not isinstance(address, dict):
            continue
        city = collapse_whitespace(address.get("ort"))
        region = collapse_whitespace(address.get("region"))
        country = collapse_whitespace(address.get("land"))
        raw = ", ".join(part for part in (city, region, country) if part)
        if not raw:
            continue
        locations.append(Location(raw=raw, city=city, region=region, country=country))
    return locations


def _salary(item: dict) -> SalaryQuote | None:
    low = _decimal(item.get("gehaltsspanneVon"))
    high = _decimal(item.get("gehaltsspanneBis"))
    if low is None and high is None:
        return None
    return SalaryQuote(
        amount_min=low,
        amount_max=high,
        # The Bundesagentur publishes German vacancies and quotes them in euro; this is a property
        # of the source rather than an inference from the number.
        currency="EUR",
        period=_PERIOD.get(str(item.get("verguetungsangabe") or ""), SalaryPeriod.UNKNOWN),
    )


def _employment_hint(item: dict) -> str | None:
    if item.get("istGeringfuegigeBeschaeftigung"):
        return "geringfuegig"
    if item.get("arbeitszeitVollzeit"):
        return "vollzeit"
    if any(
        item.get(key)
        for key in (
            "arbeitszeitTeilzeitAbend",
            "arbeitszeitTeilzeitFlexibel",
            "arbeitszeitTeilzeitNachmittag",
            "arbeitszeitTeilzeitVormittag",
        )
    ):
        return "teilzeit"
    return None


def _agency_hint(detail: dict | None) -> bool | None:
    """Only the detail payload carries the agency flags.

    Absent a detail fetch the answer is genuinely unknown, so it stays None rather than False --
    which would read downstream as "confirmed not an agency". These structured flags are also far
    more reliable than matching "Zeitarbeit" in advert prose.
    """
    if detail is None:
        return None
    return bool(
        detail.get("istArbeitnehmerUeberlassung") or detail.get("istPrivateArbeitsvermittlung")
    )


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
