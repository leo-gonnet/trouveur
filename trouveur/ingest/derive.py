"""Deterministic facet derivation. Pure: no I/O, no clock, no database.

Interpretation lives here and ONLY here; nothing downstream re-parses. Never guess: an unmatched
value derives to UNKNOWN or to nothing, because a wrong facet poisons every filter that reads it
while looking exactly like data.
"""

from __future__ import annotations

import re
from decimal import Decimal

from trouveur.ingest import places, vocab
from trouveur.ingest.places import Place
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

_SEPARATOR = re.compile(r",|\s+-\s+|\s*/\s*")
# One string naming several places, as boards write them: "Berlin; Munich", "Hamburg or Berlin",
# Workday's "Munich | Berlin".
_SEVERAL = re.compile(r";|\||\s+(?:or|oder)\s+", re.IGNORECASE)
_NOISE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(term) for term in vocab.LOCATION_NOISE) + r")(?!\w)",
    re.IGNORECASE,
)
_WORD = re.compile(r"[^\W\d_]+")
_NOT_A_PLACE = frozenset(vocab.LOCATION_NOISE + vocab.REMOTE_TERMS)
# "Frankfurt an der Oder", "United States of America".
_LONGEST_NAME = 4
# A word inside text we could not parse is evidence of a town only if it names a city this big
# somewhere: "From", "Market", "Store" and "Fully" are all towns too.
_PROMINENT = 100_000

version = DERIVE_VERSION

# Austrian contracts commonly pay 14 monthly salaries.
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
    countries, regions, cities, place_ids, unplaced, location_says_remote = _places(
        item.locations
    )

    return JobFacets(
        countries=countries,
        regions=regions,
        cities=cities,
        place_ids=place_ids,
        unplaced_countries=unplaced,
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


def _places(
    locations: list[Location],
) -> tuple[list[str], list[str], list[str], list[int], list[str], bool]:
    countries: list[str] = []
    regions: list[str] = []
    cities: list[str] = []
    place_ids: list[int] = []
    unplaced: list[str] = []
    says_remote = False

    for location in locations:
        if location.region:
            region = vocab.REGIONS.get(fold(location.region))
            if region:
                regions.append(region)
        for one in _each_named(location):
            found_countries, city, found_places, found_unplaced, remote = _place(one)
            says_remote = says_remote or remote
            countries.extend(found_countries)
            if city:
                cities.append(city)
            place_ids.extend(place.id for place in found_places)
            unplaced.extend(found_unplaced)

    return (
        _unique(countries), _unique(regions), _unique(cities),
        _unique(place_ids), _unique(unplaced), says_remote,
    )


def _each_named(location: Location) -> list[Location]:
    """"Berlin; Munich" and "Hamburg or Berlin" are two places, each read on its own."""
    if location.city:
        return [location]
    pieces = [piece.strip() for piece in _SEVERAL.split(location.raw) if piece.strip()]
    if len(pieces) < 2:
        return [location]
    return [location.model_copy(update={"raw": piece}) for piece in pieces]


def _place(location: Location) -> tuple[list[str], str | None, list[Place], list[str], bool]:
    """The countries, town, places and unplaced countries one location names."""
    country = vocab.COUNTRIES.get(fold(location.country)) if location.country else None
    city = _city(location.city) if location.city else None
    remote = False

    # Only parse free text where the source gave no structure.
    if not (location.country and location.city):
        parsed_country, parsed_city, remote = _parse_free_text(location.raw)
        # A stated country we cannot read is no country: Workday states "United States of
        # America" beside a raw "Las Vegas, NV, USA" that names it plainly.
        if not country:
            country = parsed_country
        if not location.city:
            city = parsed_city

    place = None
    if city and country:
        place = places.resolve(city, country)
    elif city and _names_only_a_town(location):
        place = places.resolve_anywhere(city)
        country = place.country if place else None

    if place:
        return [country], city, [place], [], remote
    if country:
        # Known country, unknown town: the location filter can only judge it by the country.
        return [country], city, [], [country], remote
    if location.country:
        # A country the source stated and we could not read may be exactly what says which of
        # several namesakes the town is.
        return [], city, [], [], remote
    stated, found, possible = _mentioned(location.raw)
    return stated + [p.country for p in found], city, found, possible, remote


def _mentioned(raw: str) -> tuple[list[str], list[Place], list[str]]:
    """Every country and town a location we could not parse mentions: "Munich, Bavaria",
    "Brunswick (Germany)", "Moorgate London".

    Never a guess among namesakes. A town that could be several places adds every country it could
    be in to the unplaced countries, so "Geneva" is judged as Switzerland-or-the-US: it still
    reaches anyone looking in either, and no longer reaches someone looking in Vienna.
    """
    stated: list[str] = []
    towns: list[tuple[str, tuple[Place, ...]]] = []
    # A word naming only small towns is an ordinary word as often as a place ("Fully", "Store",
    # "Market"). It may add countries a posting could be in, never be the reason it is left out.
    weak: list[tuple[Place, ...]] = []
    for part in _parts(raw):
        if fold(part) in _NOT_A_PLACE:
            continue
        found = () if _is_code(part) else places.named(part)
        if found:
            towns.append((part, found))
            continue
        for text in _names_in(part):
            key = fold(text)
            found = places.named(text)
            if len(key) > 2 and key in vocab.US_STATES:
                stated.append("US")
            # "USA" and "AT" are also spellings of towns somewhere; a capital code is the country.
            elif key in vocab.COUNTRIES and (_is_code(text) or not found):
                stated.append(vocab.COUNTRIES[key])
            elif max((p.population for p in found), default=0) >= _PROMINENT:
                towns.append((text, found))
            else:
                weak.append(found)
    if not stated and not towns:
        return [], [], []

    found_places: list[Place] = []
    possible: list[str] = []
    for found in weak:
        if not any(p.country in stated for p in found):
            possible.extend(p.country for p in found)
    for text, found in towns:
        place = next(filter(None, (places.resolve(text, code) for code in stated)), None)
        if place is None and not any(p.country in stated for p in found):
            if len(found) == 1 and not stated:
                place = found[0]
            else:
                possible.extend(p.country for p in found)
        if place:
            found_places.append(place)
    placed = {place.country for place in found_places}
    return stated, found_places, possible + [code for code in stated if code not in placed]


def _names_in(part: str) -> list[str]:
    """The longest runs of up to four words in `part` that could each be a name.

    A lowercase word is prose ("an allen Standorten"), and a short capital one is a code: "HR
    Bengaluru" is human resources, not Croatia, and "SWAN" is a company, not a town in Armenia.
    """
    words = list(_WORD.finditer(part))
    names: list[str] = []
    start = 0
    while start < len(words):
        for size in range(min(_LONGEST_NAME, len(words) - start), 0, -1):
            text = part[words[start].start() : words[start + size - 1].end()]
            key = fold(text)
            if key in _NOT_A_PLACE:
                break
            if not text[0].isupper():
                continue
            is_code = _is_code(text)
            if (key in vocab.COUNTRIES and (len(key) > 3 or is_code)) or (
                not is_code and len(key) > 2
                and (key in vocab.US_STATES or places.named(text))
            ):
                names.append(text)
                break
        else:
            size = 1
        start += size
    return names


def _is_code(text: str) -> bool:
    return len(text) <= 4 and text.isupper()


def _names_only_a_town(location: Location) -> bool:
    """A bare "London" may be looked up worldwide; "Vienna, VA" may not, because the part we
    cannot read is exactly what says which Vienna it is. So is a country the source stated and we
    could not read."""
    named = [part for part in _parts(location.raw) if fold(part) not in vocab.REMOTE_TERMS]
    return not location.region and not location.country and len(named) == 1


def _parts(raw: str) -> list[str]:
    """'UK - London' and 'Berlin, Berlin' are two ways of writing one place each. A bare hyphen
    is not a separator, or 'Castrop-Rauxel' would be two towns. 'Berlin Office' is Berlin."""
    parts: dict[str, str] = {}
    for part in _SEPARATOR.split(raw):
        part = " ".join(_NOISE.sub(" ", part).split())
        if part:
            parts.setdefault(fold(part), part)
    return list(parts.values())


def _parse_free_text(raw: str) -> tuple[str | None, str | None, bool]:
    """Read a free-text location such as 'Remote, Canada' or 'AT, Vienna'.

    The country is NOT always last -- Workday writes 'AT, Vienna' -- so it is looked for wherever
    it sits, from the end, which keeps 'Georgia, US' resolving to US.
    """
    parts = _parts(raw)
    if not parts:
        return None, None, False

    remote = any(fold(part) in vocab.REMOTE_TERMS for part in parts)
    named = [part for part in parts if fold(part) not in vocab.REMOTE_TERMS]

    country = None
    for index in range(len(named) - 1, -1, -1):
        code = vocab.COUNTRIES.get(fold(named[index]))
        if code:
            country = code
            named = named[:index] + named[index + 1 :]
            break

    states = [part for part in named[1:] if fold(part) in vocab.US_STATES]
    if states and country in (None, "US"):
        named = [part for part in named if part not in states]
        if country is None and named and places.exists(named[0], "US"):
            country = "US"
    return country, (_city(named[0]) if named else None), remote


def _location_text(locations: list[Location]) -> str:
    return fold(" ".join(location.raw for location in locations))


def _city(value: str) -> str:
    """Drop the disambiguating suffix Arbeitsagentur appends: 'Heidelberg, Neckar' -> Heidelberg."""
    return value.split(",")[0].strip()


def _work_mode(
    item: CanonicalJob, title: str, location_text: str, location_says_remote: bool
) -> WorkMode:
    """Structured hint, then the location, then the title. NEVER the description: an all-remote
    employer's boilerplate marked an explicitly Bangalore-based role remote."""
    if item.remote_hint is True:
        return WorkMode.REMOTE
    if location_says_remote:
        return WorkMode.REMOTE

    haystack = f"{title} {location_text}"
    for terms, mode in vocab.WORK_MODE_BY_TERM:
        if any(term in haystack for term in terms):
            return mode
    # False is the source stating there is no home office, which is a real answer.
    if item.remote_hint is False:
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


def _seniority(title: str) -> Seniority:
    """Title only: descriptions mention seniority in requirements and classify the wrong thing."""
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
    # A conversion would need an exchange rate, and a stale rate is a wrong number that looks
    # right. Only euro amounts become euro facets.
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


def _unique[T](values: list[T]) -> list[T]:
    seen: dict[T, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return list(seen)
