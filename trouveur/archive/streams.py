"""What gets exported, in what shape, and when a day is safe to freeze.

Three streams, because the corpus has three kinds of fact that do not age the same way: raw
`documents` by fetch day (the only irrecoverable one), `jobs` by first-seen day, and `lifecycle`
retirements by the day they happened -- which a `jobs` partition cannot carry, since a posting
closed six months later would have to reach back into an old file.

Each declares its own Arrow schema rather than inferring one: inference reads types off the first
chunk, so an all-NULL column on a quiet day lands as `null` and stops concatenating.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date

import pyarrow as pa
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.queries import archive as archive_q

_TS = pa.timestamp("us", tz="UTC")

DOCUMENT_SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("source", pa.string()),
        ("external_id", pa.string()),
        ("kind", pa.string()),
        ("scope", pa.string()),
        # Text, not a struct: a schema that unifies every source's shape is a normaliser, which
        # is the stage this stream exists to be independent of.
        ("payload", pa.string()),
        ("payload_sha256", pa.string()),
        ("fetched_at", _TS),
    ]
)

JOB_SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("public_id", pa.string()),
        ("source", pa.string()),
        ("external_id", pa.string()),
        ("scope", pa.string()),
        ("url", pa.string()),
        ("title", pa.string()),
        ("company", pa.string()),
        ("description", pa.string()),
        ("posted_at", _TS),
        ("updated_at", _TS),
        ("closes_at", _TS),
        ("locations", pa.string()),
        ("location_text", pa.string()),
        ("salary_amount_min", pa.float64()),
        ("salary_amount_max", pa.float64()),
        ("salary_currency", pa.string()),
        ("salary_period", pa.string()),
        ("remote_hint", pa.bool_()),
        ("employment_type_hint", pa.string()),
        ("agency_hint", pa.bool_()),
        ("language_hint", pa.string()),
        ("department_hint", pa.string()),
        ("content_hash", pa.string()),
        ("normalize_version", pa.int32()),
        ("listing_document_id", pa.int64()),
        ("detail_document_id", pa.int64()),
        ("first_seen_at", _TS),
        ("last_seen_at", _TS),
        ("closed_at", _TS),
        ("dedup_group", pa.string()),
        ("dedup_version", pa.int32()),
        ("derive_version", pa.int32()),
        ("countries", pa.list_(pa.string())),
        ("regions", pa.list_(pa.string())),
        ("cities", pa.list_(pa.string())),
        ("work_mode", pa.string()),
        ("seniority", pa.string()),
        ("employment_type", pa.string()),
        ("salary_min_eur_year", pa.float64()),
        ("salary_max_eur_year", pa.float64()),
        ("salary_annualised", pa.bool_()),
        ("language", pa.string()),
        ("is_agency", pa.bool_()),
        ("skills", pa.list_(pa.string())),
        ("derived_at", _TS),
    ]
)

LIFECYCLE_SCHEMA = pa.schema(
    [
        ("job_id", pa.int64()),
        ("source", pa.string()),
        ("external_id", pa.string()),
        ("last_seen_at", _TS),
        ("closed_at", _TS),
    ]
)

Page = Callable[..., Awaitable[Sequence[sa.Row]]]
Counts = Callable[[AsyncConnection], Awaitable[dict[date, int]]]


@dataclass(frozen=True)
class Stream:
    name: str
    schema: pa.Schema
    counts: Counts
    page: Page
    # Named, not inferred: paging is keyset, and a cursor on the wrong column either loops
    # forever or skips rows, both without an error.
    key: str
    # Days to leave alone at the recent end. Zero where upstream only ever inserts.
    lag_days: int

    def path(self, day: date) -> str:
        return f"{self.name}/{day.isoformat()}.parquet"


DOCUMENTS = Stream(
    name="documents",
    schema=DOCUMENT_SCHEMA,
    counts=archive_q.document_counts,
    page=archive_q.documents,
    key="id",
    lag_days=0,
)

JOBS = Stream(
    name="jobs",
    schema=JOB_SCHEMA,
    counts=archive_q.job_counts,
    page=archive_q.jobs,
    key="id",
    lag_days=3,
)

LIFECYCLE = Stream(
    name="lifecycle",
    schema=LIFECYCLE_SCHEMA,
    counts=archive_q.closure_counts,
    page=archive_q.closures,
    key="job_id",
    lag_days=0,
)

ALL = (DOCUMENTS, JOBS, LIFECYCLE)


def by_name(names: Sequence[str]) -> tuple[Stream, ...]:
    known = {stream.name: stream for stream in ALL}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(
            f"No such stream: {', '.join(unknown)}. Known streams are "
            f"{', '.join(sorted(known))}."
        )
    return tuple(known[name] for name in names)
