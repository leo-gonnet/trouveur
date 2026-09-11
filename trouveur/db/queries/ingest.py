"""Ingest-side SQL: the raw archive, job upserts and lifecycle.

Every statement here is batched. At tens of thousands of documents a sweep the round trip is the
bottleneck, not the work, and a per-row loop is the difference between a sweep that finishes in
minutes and one that does not finish.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.schema import job, job_facet, source_document, source_sweep
from trouveur.models import CanonicalJob, DocumentKind, RawDocument

log = logging.getLogger(__name__)


async def archive_documents(
    conn: AsyncConnection, documents: Sequence[RawDocument]
) -> dict[tuple[str, str], int]:
    """Store payloads verbatim. Returns {(external_id, kind): document_id}.

    Keyed by content hash, so re-fetching a posting whose bytes have not changed adds no row and
    only refreshes fetched_at. DO UPDATE rather than DO NOTHING purely so that the id comes back
    for unchanged documents too -- DO NOTHING returns nothing, and the caller needs the id to
    point the job row at its source payload.
    """
    if not documents:
        return {}
    stmt = pg_insert(source_document).values(
        [
            {
                "source": doc.source,
                "external_id": doc.external_id,
                "kind": doc.kind.value,
                "scope": doc.scope,
                "payload": doc.payload,
                "payload_sha256": doc.payload_sha256,
                "fetched_at": doc.fetched_at,
            }
            for doc in documents
        ]
    )
    stmt = stmt.on_conflict_do_update(
        constraint="source_document_identity_uniq",
        set_={"fetched_at": stmt.excluded.fetched_at},
    ).returning(
        source_document.c.id, source_document.c.external_id, source_document.c.kind
    )
    return {(row.external_id, row.kind): row.id for row in await conn.execute(stmt)}


async def latest_documents(
    conn: AsyncConnection, source: str, external_ids: Sequence[str], kind: DocumentKind
) -> dict[str, tuple[int, dict]]:
    """Most recent archived payload per posting: {external_id: (doc_id, payload, scope)}.

    Normalisation reads from here rather than from whatever the sweep happened to be holding, so
    that it always sees the complete picture. Normalising a listing-only payload for a posting
    whose description arrived in an earlier detail fetch would recompute content_hash without the
    description and flap it on every sweep, re-scoring the whole corpus daily at the user's cost.
    """
    if not external_ids:
        return {}
    stmt = (
        sa.select(
            source_document.c.id,
            source_document.c.external_id,
            source_document.c.payload,
            source_document.c.scope,
        )
        .distinct(source_document.c.external_id)
        .where(
            source_document.c.source == source,
            source_document.c.kind == kind.value,
            source_document.c.external_id.in_(list(external_ids)),
        )
        .order_by(source_document.c.external_id, source_document.c.fetched_at.desc())
    )
    return {
        row.external_id: (row.id, row.payload, row.scope) for row in await conn.execute(stmt)
    }


async def stored_content_hashes(
    conn: AsyncConnection, source: str, external_ids: Sequence[str]
) -> dict[str, tuple[int, bytes]]:
    if not external_ids:
        return {}
    stmt = sa.select(job.c.id, job.c.external_id, job.c.content_hash).where(
        job.c.source == source, job.c.external_id.in_(list(external_ids))
    )
    return {row.external_id: (row.id, row.content_hash) for row in await conn.execute(stmt)}


def job_row(
    item: CanonicalJob,
    normalize_version: int,
    scope: str | None,
    listing_document_id: int | None,
    detail_document_id: int | None,
) -> dict:
    salary = item.salary
    return {
        "source": item.source,
        "external_id": item.external_id,
        "scope": scope,
        "url": item.url,
        "title": item.title,
        "company": item.company,
        "description": item.description,
        "posted_at": item.posted_at,
        "updated_at": item.updated_at,
        "closes_at": item.closes_at,
        "locations": [loc.model_dump() for loc in item.locations],
        # Flattened for search. Cities are how people actually look for jobs, and without this
        # neither search path can see a place name at all.
        "location_text": " ".join(loc.raw for loc in item.locations),
        "salary_amount_min": salary.amount_min if salary else None,
        "salary_amount_max": salary.amount_max if salary else None,
        "salary_currency": salary.currency if salary else None,
        "salary_period": (salary.period.value if salary else "UNKNOWN"),
        "remote_hint": item.remote_hint,
        "employment_type_hint": item.employment_type_hint,
        "agency_hint": item.agency_hint,
        "language_hint": item.language_hint,
        "department_hint": item.department_hint,
        "content_hash": item.content_hash,
        "normalize_version": normalize_version,
        "listing_document_id": listing_document_id,
        "detail_document_id": detail_document_id,
    }


async def upsert_jobs(
    conn: AsyncConnection, rows: Sequence[dict]
) -> dict[str, int]:
    """Insert or refresh postings, keyed on provenance identity. Returns {external_id: job_id}.

    Seeing a posting is what makes it live, so this clears closed_at: a board that re-lists a
    vacancy reopens it, and the embedding is re-queued by the caller because closing deleted it.
    """
    if not rows:
        return {}
    stmt = pg_insert(job).values(list(rows))
    refreshable = {
        column: getattr(stmt.excluded, column)
        for column in (
            "scope", "url", "title", "company", "description", "posted_at", "updated_at",
            "closes_at", "locations", "location_text", "salary_amount_min", "salary_amount_max",
            "salary_currency", "salary_period", "remote_hint", "employment_type_hint",
            "agency_hint", "language_hint", "department_hint", "content_hash",
            "normalize_version", "listing_document_id", "detail_document_id",
        )
    }
    stmt = stmt.on_conflict_do_update(
        constraint="job_provenance_uniq",
        set_={**refreshable, "last_seen_at": sa.func.now(), "closed_at": None},
    ).returning(job.c.id, job.c.external_id)
    return {row.external_id: row.id for row in await conn.execute(stmt)}


async def ensure_facet_rows(conn: AsyncConnection, job_ids: Sequence[int]) -> None:
    """Give every job a facet row at derive_version 0.

    Created up front so that refilling the derive queue after a version bump is an index scan on
    one column rather than an anti-join against the whole job table.
    """
    if not job_ids:
        return
    stmt = pg_insert(job_facet).values([{"job_id": job_id} for job_id in job_ids])
    await conn.execute(stmt.on_conflict_do_nothing(index_elements=[job_facet.c.job_id]))


async def touch_seen(conn: AsyncConnection, job_ids: Sequence[int]) -> None:
    if not job_ids:
        return
    await conn.execute(
        job.update().where(job.c.id.in_(list(job_ids))).values(last_seen_at=sa.func.now())
    )


# Closing a job also drops its embedding, in one statement so the two can never disagree.
# job_embedding holds open postings only -- that is what keeps the ANN index proportional to the
# live corpus instead of to all history, and it is an invariant, not an optimisation.
_CLOSE_SQL = """
WITH closed AS (
    UPDATE job SET closed_at = now()
    WHERE source = :source
      AND closed_at IS NULL
      AND last_seen_at < {cutoff}
      {scope_clause}
    RETURNING id
), dropped AS (
    DELETE FROM job_embedding WHERE job_id IN (SELECT id FROM closed)
)
SELECT count(*) AS closed FROM closed
"""


async def close_unseen(
    conn: AsyncConnection, source: str, scopes: Sequence[str], sweep_started_at: datetime
) -> int:
    """Retire postings a complete sweep did not see.

    Only ever called with scopes the sweep actually observed in full. A delta sweep reports none,
    so it closes nothing: seeing only what was published yesterday says nothing about whether a
    posting from last month is still live, and closing on that basis would retire the entire
    corpus on the first run.
    """
    if not scopes:
        return 0
    sql = _CLOSE_SQL.format(
        cutoff="CAST(:cutoff AS timestamptz)",
        scope_clause="AND scope = ANY(CAST(:scopes AS text[]))",
    )
    result = await conn.execute(
        sa.text(sql),
        {"source": source, "cutoff": sweep_started_at, "scopes": list(scopes)},
    )
    return int(result.scalar_one() or 0)


async def close_stale(conn: AsyncConnection, source: str, older_than: timedelta) -> int:
    """Retire postings not seen for a long time, for sources that cannot report completeness.

    This is a heuristic and is deliberately named as one. Arbeitsagentur only ever exposes a delta,
    so nothing it returns can prove a posting has gone; without this the open set -- and therefore
    the ANN index -- would grow without bound. Age is a poor proxy for "closed", so keep the
    window generous. The principled replacement is a full partitioned sweep or per-posting
    liveness probing, neither of which is built.
    """
    sql = _CLOSE_SQL.format(cutoff="now() - CAST(:age AS interval)", scope_clause="")
    result = await conn.execute(sa.text(sql), {"source": source, "age": older_than})
    return int(result.scalar_one() or 0)


async def close_retired(conn: AsyncConnection, job_ids: Sequence[int]) -> int:
    """Close postings whose source reported them gone (a 404 on the detail endpoint).

    This is evidence rather than the age heuristic: the posting itself said it no longer exists.
    Deletes the embedding in the same statement, keeping job_embedding an index of the live set.
    """
    if not job_ids:
        return 0
    result = await conn.execute(
        sa.text(
            """
            WITH closed AS (
                UPDATE job SET closed_at = now()
                WHERE id = ANY(CAST(:ids AS bigint[])) AND closed_at IS NULL
                RETURNING id
            ), dropped AS (
                DELETE FROM job_embedding WHERE job_id IN (SELECT id FROM closed)
            )
            SELECT count(*) FROM closed
            """
        ),
        {"ids": list(job_ids)},
    )
    return int(result.scalar_one() or 0)


async def start_sweep(conn: AsyncConnection, source: str) -> tuple[int, datetime]:
    row = (
        await conn.execute(
            source_sweep.insert()
            .values(source=source)
            .returning(source_sweep.c.id, source_sweep.c.started_at)
        )
    ).one()
    return row.id, row.started_at


async def record_sweep_progress(
    conn: AsyncConnection, sweep_id: int, *, documents_seen: int
) -> None:
    """Publish how far a sweep has got, while it is still going.

    Everything else about a sweep is written when it ends, which is exactly when it stops being
    interesting: until then the row says only that the source started. A source that has been
    running for twenty minutes and one that is wedged look identical without this.
    """
    await conn.execute(
        source_sweep.update()
        .where(source_sweep.c.id == sweep_id)
        .values(documents_seen=documents_seen)
    )


async def finish_sweep(
    conn: AsyncConnection,
    sweep_id: int,
    *,
    ok: bool,
    complete: bool,
    partitions_total: int,
    partitions_done: int,
    partitions_overflowed: int,
    documents_seen: int,
    jobs_upserted: int,
    jobs_closed: int,
    error: str | None,
) -> None:
    await conn.execute(
        source_sweep.update()
        .where(source_sweep.c.id == sweep_id)
        .values(
            finished_at=sa.func.now(),
            ok=ok,
            complete=complete,
            partitions_total=partitions_total,
            partitions_done=partitions_done,
            partitions_overflowed=partitions_overflowed,
            documents_seen=documents_seen,
            jobs_upserted=jobs_upserted,
            jobs_closed=jobs_closed,
            error=error,
        )
    )
