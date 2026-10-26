"""freehire's public API as a lead source: the boards behind this week's jobs in our readers'
countries. Weekly at most, and never a corpus source.

Not archived. One lead per board, carrying the few fields a later resolver could use
(`payload`), so a new resolver rule re-reads every board we were shown without a request.

Probed 2026-10-26:
- `/jobs` ignores every filter and answers with the whole catalogue (6.3M jobs). Only
  `/jobs/search` filters, and it names a filter it ignored in `meta.ignored_params`.
- One query pages to `offset + limit` = 10,000 at most (a 400 past it), so we ask per
  (country, platform), sized by `/jobs/facets`.
- No key needed; public reads are for personal, non-commercial use. We keep board links only.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from trouveur import coverage
from trouveur.config import Settings, get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import discovery as discovery_q
from trouveur.db.queries import ingest as ingest_q
from trouveur.discovery.work import record_leads
from trouveur.models import Lead, LeadOrigin
from trouveur.sources.errors import FetchError, SourceBlocked
from trouveur.sources.feed import require_list
from trouveur.sources.http import PoliteClient
from trouveur.sources.registry import SOURCES

log = logging.getLogger(__name__)

SOURCE = "freehire"
API = "https://freehire.me/api/v1"
PAGE = 100
WINDOW = 10_000
EVERY = timedelta(days=7)
MAX_DAYS = 30

_KEPT = ("source", "external_id", "company_slug", "countries", "cities")


@dataclass
class Harvest:
    requests: int = 0
    jobs: int = 0
    queries_planned: int = 0
    queries_done: int = 0
    leads: list[Lead] = field(default_factory=list)
    blocked: str | None = None
    error: str | None = None


def board_key(item: dict) -> str | None:
    """The board a job sits on, as freehire names it: `platform:tenant`."""
    platform = item.get("source")
    external = str(item.get("external_id") or "")
    tenant = external.split(":", 1)[0] if ":" in external else item.get("company_slug")
    if not platform or not tenant:
        return None
    return f"{platform}:{tenant}".lower()


def lead_for(item: dict) -> Lead | None:
    key = board_key(item)
    url = item.get("url")
    if not key or not isinstance(url, str) or not url:
        return None
    return Lead(
        origin=LeadOrigin.FREEHIRE,
        url=url,
        company=item.get("company") or None,
        title=item.get("title") or None,
        location_text=item.get("location") or None,
        payload={"board": key, **{name: item[name] for name in _KEPT if item.get(name)}},
    )


def plan(counts: dict[str, int]) -> list[str]:
    """Which platforms to page, in order: those we can sweep first, then the smallest.

    A platform we sweep whole (an aggregator with no boards) brings no board, so it is skipped.
    """
    ours = {name for name, spec in SOURCES.items() if spec.tenant_scoped}
    whole = {name for name, spec in SOURCES.items() if not spec.tenant_scoped}
    wanted = [name for name, count in counts.items() if count > 0 and name not in whole]
    return sorted(wanted, key=lambda name: (name not in ours, counts[name], name))


def window_days(last: datetime | None, now: datetime) -> int:
    if last is None:
        return MAX_DAYS
    return max(7, min(MAX_DAYS, (now - last).days + 1))


async def harvest(
    client: PoliteClient,
    countries: Iterable[str],
    *,
    days: int,
    max_requests: int,
    known: set[str],
) -> Harvest:
    """Page this window's jobs per country and platform; one lead per board not seen before.

    Stops at the request cap or the first block, keeping what it found.
    """
    found = Harvest()
    seen = set(known)

    async def get(path: str, params: dict[str, Any]) -> Any:
        found.requests += 1
        payload = await client.get_json(f"{API}{path}", params=params)
        # A dropped filter answers with the whole catalogue and looks like a broad result.
        ignored = ((payload or {}).get("meta") or {}).get("ignored_params")
        if ignored:
            raise FetchError(f"freehire ignored the filter(s) {ignored}; the API has changed.")
        return payload

    async def page_through(scope: dict[str, Any], count: int) -> bool:
        """Every page of one query. False when the request cap stopped it."""
        for offset in range(0, min(count, WINDOW), PAGE):
            if found.requests >= max_requests:
                return False
            page = await get("/jobs/search", {**scope, "limit": PAGE, "offset": offset})
            items = require_list(page, "data", source=SOURCE)
            found.jobs += len(items)
            for item in items:
                lead = lead_for(item)
                if lead and lead.payload["board"] not in seen:
                    seen.add(lead.payload["board"])
                    found.leads.append(lead)
            if len(items) < PAGE:
                break
        return True

    try:
        for country in sorted(set(countries)):
            if found.requests >= max_requests:
                break
            scope = {"countries": country, "open_within_days": days}
            facets = await get("/jobs/facets", scope)
            counts = (((facets or {}).get("data") or {}).get("facets") or {}).get("source") or {}
            counts = {str(name): int(count) for name, count in counts.items()}
            platforms = plan(counts)
            found.queries_planned += len(platforms)
            for platform in platforms:
                if not await page_through({**scope, "source": platform}, counts[platform]):
                    break
                found.queries_done += 1
    except SourceBlocked as exc:
        found.blocked = str(exc)
        log.warning("freehire blocked us, stopping: %s", exc)
    except FetchError as exc:
        found.error = str(exc)
        log.warning("freehire pass stopped: %s", exc)
    return found


async def run(settings: Settings | None = None, *, force: bool = False) -> Harvest | None:
    """One pass, when a week has gone by since the last one. Returns None when not due."""
    settings = settings or get_settings()
    now = datetime.now(UTC)
    async with connect() as conn:
        last = await ingest_q.last_sweep_started(conn, SOURCE)
        if not force and last is not None and now - last < EVERY:
            return None
        countries = await coverage.reader_countries(conn)
        known = await discovery_q.payload_values(conn, origin=SOURCE, key="board")
        sweep_id, _ = await ingest_q.start_sweep(conn, SOURCE)

    found = Harvest()
    if countries:
        async with PoliteClient() as client:
            found = await harvest(
                client,
                countries,
                days=window_days(last, now),
                max_requests=settings.freehire_max_requests,
                known=known,
            )
    written = await record_leads(found.leads)

    async with connect() as conn:
        await ingest_q.finish_sweep(
            conn,
            sweep_id,
            ok=not (found.blocked or found.error),
            complete=False,
            partitions_total=found.queries_planned,
            partitions_done=found.queries_done,
            partitions_overflowed=0,
            documents_seen=found.jobs,
            jobs_upserted=0,
            jobs_closed=0,
            error=found.blocked or found.error,
            blocked=found.blocked is not None,
        )
    log.info(
        "freehire: %d request(s), %d job(s), %d new board lead(s)",
        found.requests, found.jobs, written,
    )
    return found
