"""Deterministic facet derivation. Pure: no I/O, no clock, no database.

Interpretation lives here and only here. Everything downstream reads facets and never re-parses a
location or re-extracts a skill, because two implementations of one question diverge, and the
divergence surfaces as a filter and a score disagreeing about the same posting.

The governing rule is: never guess. Every unmatched value derives to UNKNOWN or to nothing at all.
A missing facet costs a little recall in one filter; a wrong one silently poisons every filter and
every ranking that touches it while looking exactly like data.
"""

from __future__ import annotations

import re
from decimal import Decimal

from trouveur.ingest import vocab
from trouveur.models import (
    CanonicalJob,
    EmploymentType,
    JobFacets,
    Location,
    SalaryPeriod,
    Seniority,
    WorkMode,
    fold,
)
from trouveur.versions import DERIVE_VERSION

version = DERIVE_VERSION

# Deliberately generous: this only decides whether a posting clears a salary floor, and
# over-estimating lets a borderline job through to be judged on its merits, while
# under-estimating silently hides it. Austrian contracts commonly pay 14 monthly salaries.
_MONTHS_PER_YEAR = {"AT": 14}
_DEFAULT_MONTHS = 12
_WEEKS_PER_YEAR = 52
_DAYS_PER_YEAR = 220
_HOURS_PER_YEAR = 1800

_SKILL_PATTERN = re.compile(
    "|".join(rf"(?<!\w){re.escape(fold(skill))}(?!\w)" for skill in vocab.SKILLS)
)


def derive(item: CanonicalJob) -> JobFacets:
    title = fold(item.title)
    description = fold(item.description or "")
    countries, regions, cities, location_says_remote = _places(item.locations)

    return JobFacets(
        countries=countries,
        regions=regions,
        cities=cities,
        work_mode=_work_mode(item, title, _location_text(item.locations), location_says_remote),
        seniority=_seniority(title),
        employment_type=_employment_type(item, title),
        salary_min_eur_year=_annualise(item, countries, minimum=True),
        salary_max_eur_year=_annualise(item, countries, minimum=False),
        salary_annualised=_is_annualised(item),
        language=item.language_hint,
        is_agency=item.agency_hint,
        skills=sorted(set(_SKILL_PATTERN.findall(f"{title} {description}"))),
    )


def _places(locations: list[Location]) -> tuple[list[str], list[str], list[str], bool]:
    countries: list[str] = []
    regions: list[str] = []
    cities: list[str] = []
    says_remote = False

    for location in locations:
        if location.country:
            code = vocab.COUNTRIES.get(fold(location.country))
            if code:
                countries.append(code)
        if location.region:
            region = vocab.REGIONS.get(fold(location.region))
            if region:
                regions.append(region)
        if location.city:
            cities.append(_city(location.city))

        # Only parse the free text where the source gave no structure. A source that already
        # stated its address is never second-guessed.
        if not (location.country and location.city):
            parsed_country, parsed_city, remote = _parse_free_text(location.raw)
            says_remote = says_remote or remote
            if parsed_country and not location.country:
                countries.append(parsed_country)
            if parsed_city and not location.city:
                cities.append(parsed_city)

    return _unique(countries), _unique(regions), _unique(cities), says_remote


def _parse_free_text(raw: str) -> tuple[str | None, str | None, bool]:
    """Read a free-text location such as 'Remote, Canada' or 'Bangalore, India'."""
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        return None, None, False

    remote = any(fold(part) in vocab.REMOTE_TERMS for part in parts)
    country = vocab.COUNTRIES.get(fold(parts[-1]))
    named = [part for part in parts if fold(part) not in vocab.REMOTE_TERMS]
    if country and named and fold(named[-1]) == fold(parts[-1]):
        named = named[:-1]
    return country, (_city(named[0]) if named else None), remote


def _location_text(locations: list[Location]) -> str:
    return fold(" ".join(location.raw for location in locations))


def _city(value: str) -> str:
    """Drop the disambiguating suffix Arbeitsagentur appends: 'Heidelberg, Neckar' -> Heidelberg."""
    return value.split(",")[0].strip()


def _work_mode(
    item: CanonicalJob, title: str, location_text: str, location_says_remote: bool
) -> WorkMode:
    """Structured hint, then the location, then the title. Never the description.

    Descriptions are company boilerplate as much as role description, and scanning them reads the
    employer's culture rather than this vacancy's arrangement: GitLab describing itself as an
    all-remote company marked an explicitly Bangalore-based role remote, and any employer whose
    benefits blurb mentions remote work would do the same to its on-site roles.
    """
    # A structured claim from the source outranks anything found in prose.
    if item.remote_hint is True:
        return WorkMode.REMOTE
    if location_says_remote:
        return WorkMode.REMOTE

    haystack = f"{title} {location_text}"
    for terms, mode in vocab.WORK_MODE_BY_TERM:
        if any(term in haystack for term in terms):
            return mode
    # remote_hint False is the source stating there is no home office, which is a real answer.
    if item.remote_hint is False:
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


def _seniority(title: str) -> Seniority:
    """Title only.

    Descriptions mention seniority constantly in requirements ("reporting to a senior manager",
    "you will mentor juniors"), so matching them classifies the wrong thing.
    """
    for term, level in vocab.SENIORITY_TERMS:
        if term in title:
            return level
    return Seniority.UNKNOWN


def _employment_type(item: CanonicalJob, title: str) -> EmploymentType:
    if item.employment_type_hint:
        mapped = vocab.EMPLOYMENT_HINTS.get(fold(item.employment_type_hint))
        if mapped:
            return mapped
    for term, kind in vocab.EMPLOYMENT_TERMS:
        if term in title:
            return kind
    return EmploymentType.UNKNOWN


def _multiplier(period: SalaryPeriod, countries: list[str]) -> int | None:
    if period is SalaryPeriod.YEAR:
        return 1
    if period is SalaryPeriod.MONTH:
        for country in countries:
            if country in _MONTHS_PER_YEAR:
                return _MONTHS_PER_YEAR[country]
        return _DEFAULT_MONTHS
    return {
        SalaryPeriod.WEEK: _WEEKS_PER_YEAR,
        SalaryPeriod.DAY: _DAYS_PER_YEAR,
        SalaryPeriod.HOUR: _HOURS_PER_YEAR,
    }.get(period)


def _annualise(item: CanonicalJob, countries: list[str], *, minimum: bool) -> Decimal | None:
    salary = item.salary
    if salary is None:
        return None
    # Converting a non-euro salary would need an exchange rate, and a stale rate is a wrong
    # number that looks right. Only euro amounts become euro facets.
    if salary.currency != "EUR":
        return None
    amount = salary.amount_min if minimum else salary.amount_max
    if amount is None:
        return None
    multiplier = _multiplier(salary.period, countries)
    if multiplier is None:
        return None
    return amount * multiplier


def _is_annualised(item: CanonicalJob) -> bool:
    return bool(
        item.salary
        and item.salary.currency == "EUR"
        and item.salary.period not in (SalaryPeriod.YEAR, SalaryPeriod.UNKNOWN)
    )


def _unique(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return list(seen)
