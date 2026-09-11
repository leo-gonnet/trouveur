"""The single write path from the archive into the job table.

Everything that produces or repairs a job row goes through persist(): the sweep that just archived
a listing, the worker that just archived a detail, and a re-normalisation after a version bump.
One path, so the repair path is the same code the ordinary run exercises every day.

It deliberately re-reads payloads from the archive rather than trusting what the caller is
holding. A listing-only re-sweep normalised in isolation would produce a job with no description,
recompute content_hash without it, and flap the hash on every run -- re-deriving, re-embedding and
re-scoring the entire corpus daily at the user's expense.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur import versions
from trouveur.db.queries import ingest as q
from trouveur.ingest.embed import embedding_version
from trouveur.models import DocumentKind
from trouveur.sources.registry import normalizer_for
from trouveur.work import WorkKind, enqueue

log = logging.getLogger(__name__)


@dataclass
class PersistResult:
    upserted: int = 0
    changed: list[int] = field(default_factory=list)
    unchanged: int = 0
    skipped: int = 0
    needs_detail: list[str] = field(default_factory=list)


async def persist(
    conn: AsyncConnection, source: str, external_ids: Sequence[str], *, requires_detail: bool
) -> PersistResult:
    """Normalise the archived payloads for these postings and upsert the result."""
    result = PersistResult()
    if not external_ids:
        return result

    normalize, normalize_version = normalizer_for(source)
    listings = await q.latest_documents(conn, source, external_ids, DocumentKind.LISTING)
    details = (
        await q.latest_documents(conn, source, external_ids, DocumentKind.DETAIL)
        if requires_detail
        else {}
    )
    stored = await q.stored_content_hashes(conn, source, external_ids)

    rows: list[dict] = []
    incoming: dict[str, bytes] = {}
    for external_id in external_ids:
        listing = listings.get(external_id)
        if listing is None:
            # A detail can outlive its listing in the archive only if the listing was never
            # stored, which means something upstream skipped a step rather than a posting being
            # legitimately absent.
            log.warning("%s: no archived listing for %s; skipping", source, external_id)
            result.skipped += 1
            continue
        listing_doc_id, listing_payload, scope = listing
        detail_doc_id, detail_payload, _ = details.get(external_id, (None, None, None))

        item = normalize(listing_payload, detail_payload, external_id=external_id)
        if item is None:
            result.skipped += 1
            continue

        incoming[external_id] = item.content_hash
        rows.append(
            q.job_row(
                item,
                normalize_version,
                scope=scope,
                listing_document_id=listing_doc_id,
                detail_document_id=detail_doc_id,
            )
        )
        if requires_detail and detail_doc_id is None:
            result.needs_detail.append(external_id)

    if not rows:
        return result

    job_ids = await q.upsert_jobs(conn, rows)
    result.upserted = len(job_ids)
    await q.ensure_facet_rows(conn, list(job_ids.values()))

    for external_id, job_id in job_ids.items():
        previous = stored.get(external_id)
        if previous is None or previous[1] != incoming.get(external_id):
            result.changed.append(job_id)
        else:
            result.unchanged += 1

    # Only changed content earns new derived work. Re-deriving an unchanged posting is pure cost,
    # and at this volume it is the difference between a cheap daily run and an expensive one.
    if result.changed:
        await enqueue(conn, WorkKind.DERIVE, result.changed, str(versions.DERIVE_VERSION))
        await enqueue(conn, WorkKind.DEDUP, result.changed, str(versions.DEDUP_VERSION))
    if result.needs_detail:
        detail_ids = [job_ids[eid] for eid in result.needs_detail if eid in job_ids]
        await enqueue(conn, WorkKind.DETAIL, detail_ids, "1")

    # Embedding waits for the description. Embedding a title-only Arbeitsagentur posting would
    # produce a vector that is then immediately invalidated when the detail lands, doubling the
    # embedding work for every posting from that source and polluting recall in between.
    pending_detail = {job_ids[eid] for eid in result.needs_detail if eid in job_ids}
    embeddable = [job_id for job_id in result.changed if job_id not in pending_detail]
    if embeddable:
        await enqueue(conn, WorkKind.EMBED, embeddable, embedding_version())
    return result
