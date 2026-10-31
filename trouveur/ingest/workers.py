"""Queue drainers: one per kind of deferred work.

These are also the upgrade path -- a version bump plus a refill puts the whole corpus through the
same function that ran on today's postings, so there is no separate migration script to rot.
"""

from __future__ import annotations

import asyncio
import logging
from itertools import zip_longest

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
    RawDocument,
    SalaryPeriod,
    SalaryQuote,
    dedup_key,
)
from trouveur.sources.base import GLOBAL_SCOPE
from trouveur.sources.errors import SOURCE_BURST, SOURCE_PAUSE, SourceBlocked, SourceError
from trouveur.sources.http import PoliteClient
from trouveur.sources.registry import SOURCES
from trouveur.work import (
    WorkKind,
    claim,
    claim_across_boards,
    complete,
    fail,
    pause_source,
)

log = logging.getLogger(__name__)

WORKER = "runner"

# At most this many from one board per batch, so a batch spans many boards; and this many
# requests in flight across all of them.
_DETAIL_PER_BOARD = 20
_DETAIL_AT_ONCE = 32
_SKIPPED = object()



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


# write_facets binds 16 parameters a row, and asyncpg refuses a statement over 32,767: at 2,000
# rows one more facet column would have broken every batch.
_DERIVE_PER_BATCH = 1000


async def drain_derive(conn: AsyncConnection, limit: int = _DERIVE_PER_BATCH) -> int:
    items = await claim(conn, WorkKind.DERIVE, limit, WORKER)
    if not items:
        return 0
    rows = await jobs_q.load_for_derive(conn, [item.job_id for item in items])
    facet_rows = await asyncio.to_thread(_facet_rows, rows)
    await jobs_q.write_facets(conn, facet_rows)
    await complete(conn, [item.id for item in items])
    return len(facet_rows)


def _facet_rows(rows: list[sa.Row]) -> list[dict]:
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
                "place_ids": facets.place_ids,
                "unplaced_countries": facets.unplaced_countries,
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
    return facet_rows


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
            [item["raw"] for item in (row.locations or []) if item.get("raw")],
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
    markers = [
        (row.id, dedup_key(row.title, row.company, _board(row.scope))) for row in rows
    ]
    await jobs_q.write_dedup_markers(conn, markers, versions.DEDUP_VERSION)
    await complete(conn, [item.id for item in items])
    return len(markers)


def _board(scope: str | None) -> str | None:
    """The company's own board id, when the posting came from one. Workday's scope is
    `tenant:wdN:site`, and the tenant is the company."""
    if not scope or scope == GLOBAL_SCOPE:
        return None
    return scope.split(":")[0]


async def drain_detail(
    conn: AsyncConnection, client: PoliteClient, sources: dict[str, object], limit: int = 50
) -> int:
    """Fetch descriptions for postings whose source has a separate detail phase.

    Small batches drained continuously rather than a burst inside the sweep: one polite request
    per second absorbs ~36k Arbeitsagentur postings across a day but would add ten hours to it.

    Fetched concurrently, a few per board at a time: the per-provider clock in `PoliteClient`
    still spaces each provider's requests, and only a provider budgeted per company (Workday)
    gets faster. A failure charges the posting an attempt only when it was actually asked.
    """
    items = await claim_across_boards(conn, WorkKind.DETAIL, limit, _DETAIL_PER_BOARD, WORKER)
    if not items:
        return 0
    targets = await jobs_q.detail_targets(conn, [item.job_id for item in items])
    by_job = {item.job_id: item.id for item in items}

    gate = asyncio.Semaphore(_DETAIL_AT_ONCE)
    # Why each source stopped, decided while fetching so a failing source stops being asked;
    # requests already in flight when it stops still land.
    stopped: dict[str, str] = {}
    failed_in_a_row: dict[str, int] = {}

    async def fetch(row: sa.Row) -> object:
        async with gate:
            if row.source in stopped:
                return _SKIPPED
            try:
                document = await sources[row.source].fetch_detail(
                    _paced(client, row.source), row.external_id
                )
            except SourceBlocked as exc:
                stopped.setdefault(
                    row.source, f"{row.source} paused for {SOURCE_PAUSE}: blocked. {exc}"
                )
                return exc
            except SourceError as exc:
                failed_in_a_row[row.source] = failed_in_a_row.get(row.source, 0) + 1
                if failed_in_a_row[row.source] >= SOURCE_BURST:
                    stopped.setdefault(
                        row.source,
                        f"{row.source} paused for {SOURCE_PAUSE} after {SOURCE_BURST} detail "
                        f"failures in a row; the last was: {exc}",
                    )
                return exc
            failed_in_a_row[row.source] = 0
            return document

    # One board after another, so the slots are not all held by one board waiting out its
    # own provider's interval.
    known = _round_robin([row for row in targets if row.source in sources])
    outcomes = dict(
        zip(
            (row.id for row in known),
            await asyncio.gather(*(fetch(row) for row in known)),
            strict=True,
        )
    )

    documents: list[RawDocument] = []
    fetched: dict[str, list[str]] = {}
    done: list[int] = []
    retired: list[int] = []
    for row in targets:
        if row.id not in outcomes:
            await fail(conn, [by_job[row.id]], f"no source registered for {row.source!r}")
            continue
        outcome = outcomes[row.id]
        if outcome is _SKIPPED or isinstance(outcome, SourceBlocked):
            continue
        if isinstance(outcome, SourceError):
            await fail(conn, [by_job[row.id]], str(outcome))
            continue
        if outcome is None:
            # A 404 is the posting telling us it is gone -- evidence, not the age heuristic.
            retired.append(row.id)
            done.append(by_job[row.id])
            continue
        documents.append(outcome)
        fetched.setdefault(row.source, []).append(row.external_id)
        done.append(by_job[row.id])
    for source_name, reason in stopped.items():
        held = await pause_source(
            conn, WorkKind.DETAIL, source_name, SOURCE_PAUSE, reason, list(by_job.values())
        )
        log.warning("%s (%d item(s) held back)", reason, held)

    await ingest_q.archive_documents(conn, documents)
    for source_name, external_ids in fetched.items():
        await persist(conn, source_name, external_ids, requires_detail=True)
    if retired:
        await ingest_q.close_retired(conn, retired)
    await complete(conn, done)
    return len(done)


def _round_robin(rows: list[sa.Row]) -> list[sa.Row]:
    boards: dict[tuple[str, str | None], list[sa.Row]] = {}
    for row in rows:
        boards.setdefault((row.source, row.scope), []).append(row)
    return [row for turn in zip_longest(*boards.values()) for row in turn if row is not None]


def _paced(client: PoliteClient, source: str) -> PoliteClient:
    interval = SOURCES[source].min_interval if source in SOURCES else None
    return client if interval is None else client.paced(interval)


