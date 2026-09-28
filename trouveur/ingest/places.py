"""The city list: every spelling of a town folded to one GeoNames id, with its coordinates.

Countries can be filtered because derivation folds every spelling to one ISO code first. Cities
need the same step, or `Wien`, `Vienna` and `Wien 10., Favoriten` are three different places and a
filter on one silently drops the other two. The data is GeoNames (CC BY 4.0), built by
tools/build_places.py. Editing the file changes derived output for every stored posting, so it
comes with a DERIVE_VERSION bump, exactly like ingest/vocab.py.

Pure: the file is read once and never written.
"""

from __future__ import annotations

import bisect
import gzip
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from trouveur.models import fold

# A name shared by several towns of one country resolves to the largest only when it is this many
# times bigger than the next: "Frankfurt" is Frankfurt am Main, but "Neustadt" is nobody in
# particular. Unresolved is safe -- the posting falls back to its country and still passes.
_DOMINANCE = 10

_EARTH_RADIUS_KM = 6371.0
_POSTAL_CODE = re.compile(r"^\d{4,5}\s+")
_BRACKETS = re.compile(r"\s*\([^)]*\)")
# "Wien 10." and "Wien 1100": a district number after the town.
_DISTRICT_NUMBER = re.compile(r"\s+\d{1,4}\.?$")
_SUBDIVISION = re.compile(r"\s*[-/]\s*")


@dataclass(frozen=True)
class Place:
    id: int
    name: str
    country: str
    lat: float
    lon: float
    population: int

    @property
    def label(self) -> str:
        return f"{self.name}, {self.country}"


@dataclass(frozen=True)
class _Index:
    by_id: dict[int, Place]
    by_name: dict[tuple[str, str], tuple[Place, ...]]
    by_bare_name: dict[str, tuple[Place, ...]]
    # (folded name, -population, id), sorted, for prefix search in the form.
    names: list[tuple[str, int, int]]


@lru_cache(maxsize=1)
def _index() -> _Index:
    by_id: dict[int, Place] = {}
    by_name: dict[tuple[str, str], list[Place]] = {}
    by_bare_name: dict[str, list[Place]] = {}
    names: list[tuple[str, int, int]] = []
    data = files("trouveur.ingest").joinpath("data/places.tsv.gz").read_bytes()
    for line in gzip.decompress(data).decode("utf-8").splitlines():
        pid, name, country, lat, lon, population, alternates = line.split("\t")
        place = Place(int(pid), name, country, float(lat), float(lon), int(population))
        by_id[place.id] = place
        for folded in {fold(n) for n in alternates.split("|") if n} | {fold(name)}:
            by_name.setdefault((country, folded), []).append(place)
            by_bare_name.setdefault(folded, []).append(place)
            names.append((folded, -place.population, place.id))
    names.sort()
    return _Index(
        by_id,
        {key: tuple(value) for key, value in by_name.items()},
        {key: tuple(value) for key, value in by_bare_name.items()},
        names,
    )


def get(place_id: int) -> Place | None:
    return _index().by_id.get(place_id)


def resolve(city: str, country: str) -> Place | None:
    """The one place `city` names inside `country`, or None when it cannot be told.

    A country is required: without one, "Vienna" might as well be Vienna, Virginia. Shorter forms
    are tried only when a longer one matched NOTHING; an ambiguous name stays ambiguous, because
    cutting it down can only make it more so.
    """
    by_name = _index().by_name
    for variant in _variants(city):
        candidates = by_name.get((country, fold(variant)))
        if candidates:
            return _dominant(candidates)
    return None


def exists(city: str, country: str) -> bool:
    """Whether any town of that name is in `country`, however many share it."""
    by_name = _index().by_name
    return any((country, fold(variant)) in by_name for variant in _variants(city))


def resolve_anywhere(city: str) -> Place | None:
    """The one place `city` names anywhere in the world, or None when it cannot be told.

    For a posting that names a town and nothing else. The same dominance rule decides: "Vienna"
    is Wien, ten times bigger than any namesake, while "Cambridge" is two cities and stays
    unresolved. Only sound because the list is worldwide -- against Europe alone, every "Vienna"
    would be Wien because Vienna, Virginia would not be there to compete.
    """
    by_bare_name = _index().by_bare_name
    for variant in _variants(city):
        candidates = by_bare_name.get(fold(variant))
        if candidates:
            return _dominant(candidates)
    return None


def named(name: str) -> tuple[Place, ...]:
    """Every place called exactly `name`, in any spelling, anywhere."""
    return _index().by_bare_name.get(fold(name), ())


def _variants(city: str):
    value = city.strip()
    yield value
    cleaned = _DISTRICT_NUMBER.sub("", _BRACKETS.sub("", _POSTAL_CODE.sub("", value))).strip()
    if cleaned and cleaned != value:
        yield cleaned
    # "Berlin-Mitte", "München-Schwabing". Tried last, so "Castrop-Rauxel" matches whole first.
    head = _SUBDIVISION.split(cleaned or value)[0].strip()
    if head and head != cleaned:
        yield head


def _dominant(candidates: tuple[Place, ...]) -> Place | None:
    if len(candidates) == 1:
        return candidates[0]
    first, second = sorted(candidates, key=lambda p: p.population, reverse=True)[:2]
    return first if first.population >= _DOMINANCE * max(second.population, 1) else None


def nearby(place_ids: list[int], radius_km: int) -> list[Place]:
    """Every place within `radius_km` of any of `place_ids`, the chosen places included."""
    index = _index()
    centres = [index.by_id[pid] for pid in place_ids if pid in index.by_id]
    if not centres:
        return []
    return [
        place
        for place in index.by_id.values()
        if any(_distance_km(centre, place) <= radius_km for centre in centres)
    ]


def area(place_ids: list[int], radius_km: int) -> tuple[list[int], list[str]]:
    """The ids inside the circles, and every country they reach into.

    The countries are what a posting with no resolvable town is judged by: "Germany" alone might
    be Lörrach, inside a circle drawn around Basel, so it passes rather than being dropped.
    """
    return _area(tuple(place_ids), radius_km)


# Retrieval asks once per query, and a run sends dozens with the same profile.
@lru_cache(maxsize=32)
def _area(place_ids: tuple[int, ...], radius_km: int) -> tuple[list[int], list[str]]:
    inside = nearby(list(place_ids), radius_km)
    return sorted(p.id for p in inside), sorted({p.country for p in inside})


def _distance_km(a: Place, b: Place) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def suggest(query: str, limit: int = 10) -> list[Place]:
    """Places whose name, in any spelling, starts with `query`, biggest first."""
    folded = fold(query.strip())
    if len(folded) < 2:
        return []
    index = _index()
    start = bisect.bisect_left(index.names, (folded,))
    found: dict[int, Place] = {}
    for name, _, pid in index.names[start:]:
        if not name.startswith(folded):
            break
        found.setdefault(pid, index.by_id[pid])
    return sorted(found.values(), key=lambda p: p.population, reverse=True)[:limit]
