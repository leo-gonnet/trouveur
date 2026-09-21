"""What gets exported, in what shape, and when a day is safe to freeze.

Three streams, because the corpus has three kinds of fact and they do not age the same way:

    documents   raw payloads, keyed by the day they were fetched. Insert-only upstream, so a
                day is immutable the moment it is over. This is the stream that matters: it is
                the only input that cannot be recomputed, and everything else in the database is
                a pure function of it.
    jobs        normalised postings with their facets, keyed by the day they were first seen.
                Recomputable in principle, exported because reconstructing 200k postings by
                replaying the normaliser is a chore nobody wants before an evaluation.
    lifecycle   retirements, keyed by the day they happened. The one fact a `jobs` partition
                cannot carry: a posting first seen in March and closed in September would have
                to reach back into a file written six months earlier.

A stream declares its own Arrow schema rather than inferring one. Inference reads types off
whatever happened to be in the first chunk, so a column that is all-NULL on a quiet day lands as
`null` instead of `string` and the day's file no longer matches the rest of the dataset -- which
surfaces much later, as a reader that cannot concatenate two partitions.
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
        # The payload as Postgres stored it, as text. Not a struct: every source has a different
        # shape, several change theirs without notice, and a schema that unifies them is a
        # normaliser -- which is the stage this stream exists to be independent of.
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
Span = Callable[[AsyncConnection], Awaitable[tuple[date, date] | None]]


@dataclass(frozen=True)
class Stream:
    name: str
    schema: pa.Schema
    span: Span
    page: Page
    # The column the page query orders by and resumes from. Named, not inferred from the schema:
    # paging is keyset, so reading this off field order would make reordering a schema silently
    # change which column the cursor advances on -- and a cursor on the wrong column either
    # loops forever or skips rows, both without an error.
    key: str
    # Days to leave alone at the recent end. Zero where upstream only ever inserts; a posting's
    # description arrives on a later fetch than its listing, so freezing a `jobs` day the moment
    # it is over would archive rows that are still filling in.
    lag_days: int

    def path(self, day: date) -> str:
        return f"{self.name}/{day.isoformat()}.parquet"


DOCUMENTS = Stream(
    name="documents",
    schema=DOCUMENT_SCHEMA,
    span=archive_q.document_span,
    page=archive_q.documents,
    key="id",
    lag_days=0,
)

JOBS = Stream(
    name="jobs",
    schema=JOB_SCHEMA,
    span=archive_q.job_span,
    page=archive_q.jobs,
    key="id",
    lag_days=3,
)

LIFECYCLE = Stream(
    name="lifecycle",
    schema=LIFECYCLE_SCHEMA,
    span=archive_q.closure_span,
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
