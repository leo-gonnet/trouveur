"""Queue drainers: one per kind of deferred work.

These are also the upgrade path -- a version bump plus a refill puts the whole corpus through the
same function that ran on today's postings, so there is no separate migration script to rot.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur import versions
from trouveur.db.queries import ingest as ingest_q
from trouveur.db.queries import jobs as jobs_q
from trouveur.ingest.derive import derive
from trouveur.ingest.embed import embedding_version, get_provider
from trouveur.ingest.embed.text import embedding_text
from trouveur.ingest.persist import persist
from trouveur.models import (
    CanonicalJob,
    Location,
    SalaryPeriod,
    SalaryQuote,
    dedup_key,
)
from trouveur.sources.errors import SourceError
from trouveur.sources.http import PoliteClient
from trouveur.work import WorkKind, claim, complete, fail

log = logging.getLogger(__name__)

WORKER = "runner"


def _to_canonical(row: sa.Row) -> CanonicalJob:
    """Rebuild the canonical record from stored columns, so derivation stays a pure function."""
    salary = None
    if row.salary_amount_min is not None or row.salary_amount_max is not None:
        salary = SalaryQuote(
            amount_min=row.salary_amount_min,
            amount_max=row.salary_amount_max,
            currency=row.salary_currency,
            period=SalaryPeriod(row.salary_period),
        )
    return CanonicalJob(
        source=row.source,
        external_id=row.external_id,
        url=row.url,
        title=row.title,
        company=row.company,
        description=row.description,
        locations=[Location.model_validate(item) for item in (row.locations or [])],
        salary=salary,
        remote_hint=row.remote_hint,
        employment_type_hint=row.employment_type_hint,
        agency_hint=row.agency_hint,
        language_hint=row.language_hint,
        department_hint=row.department_hint,
    )


async def drain_derive(conn: AsyncConnection, limit: int = 500) -> int:
    items = await claim(conn, WorkKind.DERIVE, limit, WORKER)
    if not items:
        return 0
    rows = await jobs_q.load_for_derive(conn, [item.job_id for item in items])
    facet_rows = []
    for row in rows:
        facets = derive(_to_canonical(row))
        facet_rows.append(
            {
                "job_id": row.id,
                "derive_version": versions.DERIVE_VERSION,
                "countries": facets.countries,
                "regions": facets.regions,
                "cities": facets.cities,
                "work_mode": facets.work_mode.value,
                "seniority": facets.seniority.value,
                "employment_type": facets.employment_type.value,
                "salary_min_eur_year": facets.salary_min_eur_year,
                "salary_max_eur_year": facets.salary_max_eur_year,
                "salary_annualised": facets.salary_annualised,
                "language": facets.language,
                "is_agency": facets.is_agency,
                "skills": facets.skills,
                "derived_at": sa.func.now(),
            }
        )
    await jobs_q.write_facets(conn, facet_rows)
    await complete(conn, [item.id for item in items])
    return len(facet_rows)


async def drain_embed(conn: AsyncConnection, limit: int = 256) -> int:
    items = await claim(conn, WorkKind.EMBED, limit, WORKER)
    if not items:
        return 0
    rows = await jobs_q.load_for_embedding(conn, [item.job_id for item in items])
    if not rows:
        # Every claimed posting closed between queue and embed. Closing deletes embeddings by
        # design, so these are done, not failed.
        await complete(conn, [item.id for item in items])
        return 0

    provider = get_provider()
    version = embedding_version()
    texts = [
        embedding_text(
            row.title,
            row.company,
            [item.get("raw", "") for item in (row.locations or [])],
            row.description,
        )
        for row in rows
    ]
    try:
        vectors = await provider.embed_documents(texts)
    except Exception as exc:  # noqa: BLE001 - one bad batch must not stop the worker
        await fail(conn, [item.id for item in items], f"embedding failed: {exc}")
        log.warning("embed batch of %d failed: %s", len(rows), exc)
        return 0

    await jobs_q.write_embeddings(
        conn, [(row.id, version, vector) for row, vector in zip(rows, vectors, strict=True)]
    )
    await complete(conn, [item.id for item in items])
    return len(rows)


async def drain_dedup(conn: AsyncConnection, limit: int = 1000) -> int:
    """Mark postings that look like the same role.

    Naive on purpose, which is why it writes a MARKER and never merges: a better pass is a
    version bump away, and a merge could not be undone.
    """
    items = await claim(conn, WorkKind.DEDUP, limit, WORKER)
    if not items:
        return 0
    rows = await jobs_q.load_for_dedup(conn, [item.job_id for item in items])
    markers = []
    for row in rows:
        locations = row.locations or []
        city = (locations[0].get("city") or locations[0].get("raw")) if locations else None
        markers.append((row.id, dedup_key(row.title, row.company, city)))
    await jobs_q.write_dedup_markers(conn, markers, versions.DEDUP_VERSION)
    await complete(conn, [item.id for item in items])
    return len(markers)


async def drain_detail(
    conn: AsyncConnection, client: PoliteClient, sources: dict[str, object], limit: int = 50
) -> int:
    """Fetch descriptions for postings whose source has a separate detail phase.

    Small batches drained continuously rather than a burst inside the sweep: one polite request
    per second absorbs ~36k Arbeitsagentur postings across a day but would add ten hours to it.
    """
    items = await claim(conn, WorkKind.DETAIL, limit, WORKER)
    if not items:
        return 0
    targets = await jobs_q.detail_targets(conn, [item.job_id for item in items])
    by_job = {item.job_id: item.id for item in items}

    fetched: dict[str, list[str]] = {}
    done: list[int] = []
    retired: list[int] = []
    for row in targets:
        source = sources.get(row.source)
        if source is None:
            await fail(conn, [by_job[row.id]], f"no source registered for {row.source!r}")
            continue
        try:
            document = await source.fetch_detail(client, row.external_id)
        except SourceError as exc:
            await fail(conn, [by_job[row.id]], str(exc))
            continue
        if document is None:
            # A 404 is the posting telling us it is gone -- evidence, not the age heuristic.
            retired.append(row.id)
            done.append(by_job[row.id])
            continue
        await ingest_q.archive_documents(conn, [document])
        fetched.setdefault(row.source, []).append(row.external_id)
        done.append(by_job[row.id])

    for source_name, external_ids in fetched.items():
        await persist(conn, source_name, external_ids, requires_detail=True)
    if retired:
        await ingest_q.close_retired(conn, retired)
    await complete(conn, done)
    return len(done)


