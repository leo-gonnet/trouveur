"""Bundesagentur für Arbeit Jobsuche API — primary source for Germany.

Public and free: no signup, the API key below is the documented public one. robots.txt does not
apply; this is a published JSON API intended for third-party use.

Read the Arbeitsagentur section of AGENTS.md before changing this file. Every constraint here was
established by probing the live API, and every one fails *silently* — zero rows or an HTTP 400,
never an exception.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trouveur.models import Job, SalaryPeriod
from trouveur.sources.http import PoliteClient

log = logging.getLogger(__name__)

_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"

# Search is v6 and detail is v4. This asymmetry is real: each endpoint returns 403 on the version
# that would look consistent. See the table in AGENTS.md.
_SEARCH_URL = f"{_BASE}/pc/v6/jobs"
_DETAIL_URL = f"{_BASE}/pc/v4/jobdetails"

_API_KEY = "jobboerse-jobsuche"
# The API rejects the library default UA; it wants something browser-shaped.
_HEADERS = {
    "X-API-Key": _API_KEY,
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
}

_PAGE_SIZE = 100
_MAX_PAGES = 25

# The API returns German country names. Unmapped values pass through raw, which the rules filter
# then rejects for not being in the target list -- correct, but it looks inconsistent on the
# dashboard, so the common neighbours are mapped too.
_COUNTRY = {
    "DEUTSCHLAND": "DE", "OESTERREICH": "AT", "SCHWEIZ": "CH",
    "LUXEMBURG": "LU", "NIEDERLANDE": "NL", "BELGIEN": "BE", "FRANKREICH": "FR",
    "ITALIEN": "IT", "POLEN": "PL", "TSCHECHIEN": "CZ", "DAENEMARK": "DK",
    "SPANIEN": "ES", "GROSSBRITANNIEN": "GB", "SCHWEDEN": "SE", "UNGARN": "HU",
    "SLOWAKEI": "SK", "SLOWENIEN": "SI", "KROATIEN": "HR", "PORTUGAL": "PT",
    "IRLAND": "IE", "NORWEGEN": "NO", "FINNLAND": "FI", "RUMAENIEN": "RO",
}
_PERIOD = {
    "JAHRESGEHALT": SalaryPeriod.YEAR,
    "MONATSGEHALT": SalaryPeriod.MONTH,
    "STUNDENLOHN": SalaryPeriod.HOUR,
}


class ArbeitsagenturSource:
    name = "arbeitsagentur"

    def __init__(self, keywords: list[str], cities: list[str] | None = None) -> None:
        self.keywords = keywords
        # Sent verbatim. Never transliterate: wo=München returns ~178 results while
        # wo=Muenchen returns 0, with no error either way.
        self.cities = cities or []

    async def fetch(self, client: PoliteClient, since: datetime | None) -> AsyncIterator[Job]:
        published_within = _days_since(since)
        seen: set[str] = set()
        for keyword in self.keywords:
            for city in self.cities or [None]:
                async for item in self._search(client, keyword, city, published_within):
                    ref = item.get("referenznummer")
                    if not ref or ref in seen:
                        continue
                    seen.add(ref)
                    job = _to_job(item)
                    if job is not None:
                        yield job

    async def _search(
        self, client: PoliteClient, keyword: str, city: str | None, published_within: int | None
    ) -> AsyncIterator[dict]:
        for page in range(1, _MAX_PAGES + 1):
            params: dict[str, Any] = {"was": keyword, "size": _PAGE_SIZE, "page": page}
            if city:
                # `wo` only. `arbeitsort` is not a synonym: arbeitsort=Wien returns 0.
                params["wo"] = city
            if published_within is not None:
                params["veroeffentlichtseit"] = published_within
            # Deliberately absent: `pav` (400), `homeoffice` (400), `arbeitszeit=ho` (0 rows).
            # Remote is filtered client-side from homeofficemoeglich below.

            payload = await client.get_json(_SEARCH_URL, params=params, headers=_HEADERS)
            # The list key is `ergebnisliste`, not `stellenangebote`.
            results = payload.get("ergebnisliste") or []
            for item in results:
                yield item
            total = payload.get("maxErgebnisse") or 0
            if len(results) < _PAGE_SIZE or page * _PAGE_SIZE >= total:
                return

    async def enrich(self, client: PoliteClient, native_id: str) -> dict:
        """Fetch the description for one posting.

        Costs one request per job, so the pipeline calls this only for rules-filter survivors.
        """
        token = base64.b64encode(native_id.encode()).decode()
        detail = await client.get_json(f"{_DETAIL_URL}/{token}", headers=_HEADERS)
        return {
            "description": detail.get("stellenangebotsBeschreibung"),
            "is_staffing_agency": bool(detail.get("istArbeitnehmerUeberlassung")),
            "is_private_agency": bool(detail.get("istPrivateArbeitsvermittlung")),
            "is_marginal_employment": bool(detail.get("istGeringfuegigeBeschaeftigung")),
            "full_time": detail.get("arbeitszeitVollzeit"),
        }


def _days_since(since: datetime | None) -> int | None:
    if since is None:
        return None
    delta = (datetime.now(UTC) - since).days
    return max(0, min(delta, 100))


def _to_job(item: dict) -> Job | None:
    ref = item.get("referenznummer")
    title = item.get("stellenangebotsTitel") or item.get("beruf")
    if not ref or not title:
        return None

    city = country = None
    locations = item.get("stellenlokationen") or []
    if locations:
        address = locations[0].get("adresse") or {}
        city = address.get("ort")
        country = _COUNTRY.get(address.get("land"), address.get("land"))

    return Job(
        source="arbeitsagentur",
        source_native_id=ref,
        url=item.get("externeURL") or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
        title=title,
        company=item.get("firma"),
        location_city=city,
        location_country=country,
        # No server-side remote filter exists, so this field is the only remote signal.
        remote=item.get("homeofficemoeglich"),
        salary_min=_decimal(item.get("gehaltsspanneVon")),
        salary_max=_decimal(item.get("gehaltsspanneBis")),
        salary_period=_PERIOD.get(item.get("verguetungsangabe"), SalaryPeriod.UNKNOWN),
        posted_at=_date(item.get("datumErsteVeroeffentlichung")),
        description=None,  # search results carry none; see enrich()
        raw=item,
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)
    except ValueError:
        return None
