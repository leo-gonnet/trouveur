"""Running discovery in bounded batches: mine the archive, resolve the leads, propose the boards.

Three stages in one pass per tick, and the order matters: mining writes leads already carrying
their resolution, resolving brings old leads up to the current rules, and promoting turns whatever
is resolved into a disabled candidate. Promoting last means a board found by either of the other
two is proposed in the same tick.

Bounded on purpose. This is a backfill that walks the whole archive once, and it shares the runner
with the work the nightly edition depends on, so it takes a slice per tick and no more.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from trouveur import versions
from trouveur.db.engine import connect
from trouveur.db.queries import discovery as q
from trouveur.discovery.mine import leads_from_posting
from trouveur.discovery.resolve import resolve
from trouveur.models import Lead
from trouveur.sources import registry

log = logging.getLogger(__name__)

# Postings per pass. Small because each row drags both of its raw payloads along, and a posting's
# payload is mostly its description.
MINE_BATCH = 200
# Leads per pass. Resolution is a regex over a URL, so this is bounded by the write, not the work.
RESOLVE_BATCH = 2000


@dataclass
class DiscoveryReport:
    mined: int = 0
    leads: int = 0
    # Old leads brought up to the current URL rules, not leads that resolved: mining resolves
    # a lead as it writes it, so this is only ever above zero after a RESOLVE_VERSION bump.
    rechecked: int = 0
    candidates: int = 0

    @property
    def moved(self) -> bool:
        return bool(self.mined or self.rechecked or self.candidates)


def _tenant_scoped() -> list[str]:
    """The sources a discovered board can be registered under. The registry is the only place
    that knows; a global source has no tenants, so a row for it would be swept by nothing."""
    return [name for name, spec in registry.SOURCES.items() if spec.tenant_scoped]


def _row(lead: Lead) -> dict:
    resolution = resolve(lead.url)
    return {
        "origin": lead.origin.value,
        "url": lead.url or "",
        "host": resolution.host,
        "job_id": lead.job_id,
        "company": lead.company,
        "title": lead.title,
        "location_text": lead.location_text,
        "result": resolution.result.value,
        "source": resolution.source,
        "scope": resolution.scope,
        "resolve_version": versions.RESOLVE_VERSION,
    }


async def record_leads(leads: list[Lead]) -> int:
    """Store leads from any source, resolved as they land. The one write path for a lead.

    Proposes the boards in the same call, because a lead from outside the runner -- a reader's
    report, an aggregator search -- otherwise waits for a tick that happens to move something else
    before its board is proposed at all, and from the reader's side that reads as nothing having
    happened.
    """
    if not leads:
        return 0
    async with connect() as conn:
        written = await q.insert_leads(conn, [_row(lead) for lead in leads])
        await q.promote_candidates(conn, _tenant_scoped())
    return written


async def mine_archive(limit: int = MINE_BATCH) -> tuple[int, int]:
    """Mine one page of not-yet-mined postings for links. Returns (postings, leads).

    The page is marked mined after its leads are written, in the same transaction: the other order
    loses a page's leads outright if the process dies between the two, and nothing would ever come
    back for them. No cursor to carry: a mined posting leaves the set this reads, so each pass
    starts at the first one still below the version.
    """
    async with connect() as conn:
        rows = await q.mining_batch(conn, version=versions.MINE_VERSION, chunk=limit)
        if not rows:
            return 0, 0
        leads = [
            lead
            for row in rows
            for lead in leads_from_posting(
                job_id=row.id,
                source=row.source,
                scope=row.scope,
                url=row.url,
                company=row.company,
                title=row.title,
                location_text=row.location_text,
                payload=row.payload,
            )
        ]
        written = await q.insert_leads(conn, [_row(lead) for lead in leads])
        await q.record_mined(conn, [row.id for row in rows], versions.MINE_VERSION)
    return len(rows), written


async def resolve_stale(limit: int = RESOLVE_BATCH) -> int:
    """Re-read one page of leads under the current rules. The whole table after a version bump.

    No cursor to carry: a re-read lead has its version written in the same transaction, so it
    leaves the set and the walk cannot stall on a page it already did.
    """
    async with connect() as conn:
        rows = await q.leads_to_resolve(conn, version=versions.RESOLVE_VERSION, chunk=limit)
        if not rows:
            return 0
        updates = []
        for row in rows:
            resolution = resolve(row.url)
            updates.append(
                {
                    "lead_id": row.id,
                    "result": resolution.result.value,
                    "source": resolution.source,
                    "scope": resolution.scope,
                    "host": resolution.host,
                    "resolve_version": versions.RESOLVE_VERSION,
                }
            )
        return await q.write_resolutions(conn, updates)


async def run_once() -> DiscoveryReport:
    """One bounded pass of every discovery stage."""
    report = DiscoveryReport()
    report.mined, report.leads = await mine_archive()
    report.rechecked = await resolve_stale()
    if report.moved:
        async with connect() as conn:
            report.candidates = await q.promote_candidates(conn, _tenant_scoped())
    if report.candidates:
        log.info(
            "discovery proposed %d new board(s) from %d lead(s)",
            report.candidates, report.leads,
        )
    return report
