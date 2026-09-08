"""Job-side SQL: reading rows for derivation and embedding, writing what those stages produce."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.schema import job, job_facet


async def load_for_derive(conn: AsyncConnection, job_ids: Sequence[int]) -> list[sa.Row]:
    if not job_ids:
        return []
    return list(
        await conn.execute(
            sa.select(
                job.c.id, job.c.source, job.c.external_id, job.c.url, job.c.title,
                job.c.company, job.c.description, job.c.locations, job.c.salary_amount_min,
                job.c.salary_amount_max, job.c.salary_currency, job.c.salary_period,
                job.c.remote_hint, job.c.employment_type_hint, job.c.agency_hint,
                job.c.language_hint, job.c.department_hint,
            ).where(job.c.id.in_(list(job_ids)))
        )
    )


async def write_facets(conn: AsyncConnection, rows: Sequence[dict]) -> None:
    if not rows:
        return
    stmt = pg_insert(job_facet).values(list(rows))
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[job_facet.c.job_id],
            set_={
                column: getattr(stmt.excluded, column)
                for column in rows[0]
                if column != "job_id"
            },
        )
    )


async def load_for_embedding(conn: AsyncConnection, job_ids: Sequence[int]) -> list[sa.Row]:
    if not job_ids:
        return []
    return list(
        await conn.execute(
            sa.select(
                job.c.id, job.c.title, job.c.company, job.c.description, job.c.locations
            ).where(job.c.id.in_(list(job_ids)), job.c.closed_at.is_(None))
        )
    )


# pgvector's halfvec has no SQLAlchemy type here, so vectors go over as text and are cast in the
# statement. unnest keeps it one round trip for the whole batch rather than one per vector.
_WRITE_EMBEDDINGS = """
INSERT INTO job_embedding (job_id, embedding_version, embedding)
SELECT id, version, vector::halfvec
FROM unnest(CAST(:ids AS bigint[]), CAST(:versions AS text[]), CAST(:vectors AS text[]))
     AS t(id, version, vector)
ON CONFLICT (job_id) DO UPDATE
SET embedding = EXCLUDED.embedding,
    embedding_version = EXCLUDED.embedding_version,
    embedded_at = now()
"""


async def write_embeddings(
    conn: AsyncConnection, rows: Sequence[tuple[int, str, list[float]]]
) -> None:
    if not rows:
        return
    await conn.execute(
        sa.text(_WRITE_EMBEDDINGS),
        {
            "ids": [job_id for job_id, _, _ in rows],
            "versions": [version for _, version, _ in rows],
            "vectors": [
                "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
                for _, _, vector in rows
            ],
        },
    )


async def load_for_dedup(conn: AsyncConnection, job_ids: Sequence[int]) -> list[sa.Row]:
    if not job_ids:
        return []
    return list(
        await conn.execute(
            sa.select(job.c.id, job.c.title, job.c.company, job.c.locations).where(
                job.c.id.in_(list(job_ids))
            )
        )
    )


async def write_dedup_markers(
    conn: AsyncConnection, rows: Sequence[tuple[int, bytes]], dedup_version: int
) -> None:
    """Record which postings look like the same role. Marks only; never merges, never deletes.

    Collapsing two rows destroys the evidence for the decision, and an early dedup pass is always
    partly wrong. A marker can be recomputed by bumping DEDUP_VERSION; a merge cannot be undone.
    """
    if not rows:
        return
    await conn.execute(
        sa.text(
            """
            UPDATE job SET dedup_group = t.grp, dedup_version = :version
            FROM unnest(CAST(:ids AS bigint[]), CAST(:groups AS bytea[])) AS t(id, grp)
            WHERE job.id = t.id
            """
        ),
        {
            "ids": [job_id for job_id, _ in rows],
            "groups": [group for _, group in rows],
            "version": dedup_version,
        },
    )


async def detail_targets(conn: AsyncConnection, job_ids: Sequence[int]) -> list[sa.Row]:
    if not job_ids:
        return []
    return list(
        await conn.execute(
            sa.select(job.c.id, job.c.source, job.c.external_id).where(job.c.id.in_(list(job_ids)))
        )
    )
