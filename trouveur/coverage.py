"""The coverage report: what we have collected for the places readers actually asked about.

Composed here rather than in `db/queries` because grouping profiles into areas is not SQL, and
read by both Operations and `trouveur coverage` so the page and the command cannot drift apart.

Later tasks add sections to it: one query in `db/queries/coverage.py`, one field on `Report`
filled in `report()`, one block in `_admin_coverage.html`. "Found it elsewhere" results come from
docs/tasks/03, blocked sources from 06, aggregator leads from 07. None of them emits an empty row
here yet: a key that is always zero is one an agent reads as an answer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur import versions
from trouveur.db.queries import coverage as coverage_q
from trouveur.db.queries import discovery as discovery_q
from trouveur.db.queries import freshness
from trouveur.ingest import places


@dataclass(frozen=True)
class Area:
    """Where a reader is looking: whole countries, towns with one radius, and remote or not.

    The same four fields the location filter reads, so it can be passed to it directly. Several
    readers share one area; none of them is named here.
    """

    countries: tuple[str, ...]
    city_ids: tuple[int, ...]
    radius_km: int
    remote_anywhere: bool
    readers: int = 1

    @property
    def cities(self) -> list[str]:
        """The picked towns, spelled by the committed city list rather than by the reader.

        An id the list no longer holds keeps its number instead of disappearing: a rebuilt
        `places.tsv.gz` can retire one, and a silently shorter area reads as a reader who picked
        fewer towns.
        """
        return [
            place.label if (place := places.get(city_id)) else str(city_id)
            for city_id in self.city_ids
        ]

    @property
    def label(self) -> str:
        parts = []
        if self.cities:
            parts.append(f"{', '.join(self.cities)} +{self.radius_km} km")
        if self.countries:
            parts.append(", ".join(self.countries))
        if not parts:
            parts.append("anywhere")
        if self.remote_anywhere:
            parts.append("remote too")
        return " · ".join(parts)


@dataclass(frozen=True)
class SourceCoverage:
    source: str
    open_jobs: int
    # Open, inside the retrieval horizon and carrying a vector: what the dense arm can return
    # today. Everything below `open_jobs` is collected but invisible to recommendations.
    retrievable_jobs: int
    # Postings no other source in this area also has, by the dedup markers. Lose the source,
    # lose these outright.
    only_source: int


@dataclass(frozen=True)
class AreaCoverage:
    area: Area
    sources: list[SourceCoverage] = field(default_factory=list)

    @property
    def open_jobs(self) -> int:
        return sum(row.open_jobs for row in self.sources)

    @property
    def retrievable_jobs(self) -> int:
        return sum(row.retrievable_jobs for row in self.sources)

    @property
    def only_source(self) -> int:
        """How much of this area rests on a single source: postings with no twin elsewhere."""
        return sum(row.only_source for row in self.sources)


@dataclass(frozen=True)
class TenantCoverage:
    source: str
    sweeping: int
    # Proposed by a discovery pass and not yet promoted.
    candidates: int
    disabled: int
    # Tried and decided against. Kept so discovery does not propose it again.
    dropped: int


@dataclass(frozen=True)
class UnreadHost:
    """A host whose links we cannot read. The count is the case for writing a rule for it."""

    host: str
    leads: int


@dataclass(frozen=True)
class LeadCoverage:
    """What discovery has found, and what each of its stages still has to get through.

    The backlogs belong beside the counts: a panel reading zero leads means one thing when the
    archive is mined and quite another when a hundred thousand postings are still waiting.
    """

    leads: int
    resolved: int
    unread: int
    unread_host_count: int
    unmined_jobs: int
    awaiting_resolve: int
    hosts: list[UnreadHost] = field(default_factory=list)


@dataclass(frozen=True)
class Report:
    generated_at: datetime
    horizon_days: int
    areas: list[AreaCoverage]
    tenants: list[TenantCoverage]
    # None until discovery has seen anything: an always-empty section reads as an answer.
    leads: LeadCoverage | None = None

    def as_dict(self) -> dict:
        """The shape `trouveur coverage --json` prints. Plain types only, for an agent to read."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "horizon_days": self.horizon_days,
            "areas": [
                {
                    "label": entry.area.label,
                    "countries": list(entry.area.countries),
                    "city_ids": list(entry.area.city_ids),
                    "cities": entry.area.cities,
                    "radius_km": entry.area.radius_km,
                    "remote_anywhere": entry.area.remote_anywhere,
                    "readers": entry.area.readers,
                    "open_jobs": entry.open_jobs,
                    "retrievable_jobs": entry.retrievable_jobs,
                    "only_source": entry.only_source,
                    "sources": [asdict(row) for row in entry.sources],
                }
                for entry in self.areas
            ],
            "tenants": [asdict(row) for row in self.tenants],
            "leads": asdict(self.leads) if self.leads else None,
        }


async def areas(conn: AsyncConnection) -> list[Area]:
    """Every distinct area an active reader asked for, busiest first.

    Two readers who picked the same countries, towns and radius see exactly one corpus, so they
    are one row. The lists are sorted into the key: `['AT','DE']` and `['DE','AT']` are one area
    and two stored values, and leaving them as the form wrote them would report it twice.
    """
    readers: dict[tuple[tuple[str, ...], tuple[int, ...], int, bool], int] = {}
    for row in await coverage_q.areas(conn):
        key = (
            tuple(sorted(row.countries or ())),
            tuple(sorted(row.city_ids or ())),
            int(row.radius_km),
            bool(row.remote_anywhere),
        )
        readers[key] = readers.get(key, 0) + 1
    return sorted(
        (
            Area(
                countries=countries, city_ids=city_ids, radius_km=radius_km,
                remote_anywhere=remote_anywhere, readers=count,
            )
            for (countries, city_ids, radius_km, remote_anywhere), count in readers.items()
        ),
        key=lambda area: (-area.readers, area.label),
    )


async def report(conn: AsyncConnection, horizon_days: int) -> Report:
    """The whole report: one filtered pass over the corpus per area, plus the crawl set once.

    Per area rather than per row, and areas are few -- one query each is the shape, not a loop
    that should have been a batch. The filter parameters differ for every area, so one statement
    could not serve them all without rebuilding the filter as a join against the profiles.
    """
    fresh_since = freshness.fresh_since(horizon_days)
    covered = []
    for area in await areas(conn):
        rows = await coverage_q.by_source(conn, area, fresh_since)
        covered.append(
            AreaCoverage(
                area=area,
                sources=[
                    SourceCoverage(
                        source=row.source,
                        open_jobs=int(row.open_jobs),
                        retrievable_jobs=int(row.retrievable_jobs),
                        only_source=int(row.only_source),
                    )
                    for row in rows
                ],
            )
        )
    return Report(
        leads=await leads(conn),
        generated_at=datetime.now(UTC),
        horizon_days=horizon_days,
        areas=covered,
        tenants=[
            TenantCoverage(
                source=row.source,
                sweeping=int(row.sweeping),
                candidates=int(row.candidates),
                disabled=int(row.disabled),
                dropped=int(row.dropped),
            )
            for row in await coverage_q.tenant_states(conn)
        ],
    )


async def leads(conn: AsyncConnection) -> LeadCoverage | None:
    """What discovery holds, or nothing at all when it has not run yet."""
    counts = await discovery_q.totals(
        conn,
        resolve_version=versions.RESOLVE_VERSION,
        mine_version=versions.MINE_VERSION,
    )
    if not counts["leads"] and not counts["unmined_jobs"]:
        return None
    return LeadCoverage(
        leads=counts["leads"],
        resolved=counts["resolved"],
        unread=counts["unread"],
        unread_host_count=counts["unread_hosts"],
        unmined_jobs=counts["unmined_jobs"],
        awaiting_resolve=counts["awaiting_resolve"],
        hosts=[
            UnreadHost(host=row.host, leads=int(row.leads))
            for row in await discovery_q.unread_hosts(conn)
        ],
    )
