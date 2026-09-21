"""Guards on the offsite export.

The export's whole correctness argument is that a day partition is immutable and written once.
Two things can break that quietly, and both are tested here: freezing a day that can still
change, and a keyset cursor that does not advance on the column the query orders by.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

# The archive is an optional extra; a checkout without it should not fail the suite.
pytest.importorskip("pyarrow")

from trouveur.archive import streams
from trouveur.db.queries import archive as archive_q


def test_every_stream_pages_on_a_column_it_projects():
    """A cursor on a column the file does not carry either loops forever or skips rows."""
    for stream in streams.ALL:
        assert stream.key in stream.schema.names, (
            f"{stream.name} resumes from {stream.key!r}, which is not in its schema"
        )


def test_every_stream_orders_by_the_column_it_resumes_from():
    """Keyset paging is only correct if the ORDER BY and the cursor are the same column."""
    sql = {
        "documents": archive_q._DOCUMENTS,
        "jobs": archive_q._JOBS,
        "lifecycle": archive_q._CLOSURES,
    }
    for stream in streams.ALL:
        text = sql[stream.name]
        order = re.search(r"ORDER BY\s+(?:\w+\.)?(\w+)", text)
        assert order, f"{stream.name} has no ORDER BY; keyset paging needs one"
        # `jobs` orders by j.id and projects it as `id`; lifecycle projects id AS job_id.
        assert order.group(1) in {stream.key, "id"}, (
            f"{stream.name} orders by {order.group(1)} but resumes from {stream.key}"
        )
        assert "OFFSET" not in text.upper(), f"{stream.name} must page by keyset, not OFFSET"


def test_the_export_never_reads_a_table_that_holds_personal_data():
    """The corpus is public job postings. An account, a credential or a match history is not.

    Asserted over the query text rather than the module, so that the comment explaining *why*
    these tables are excluded can go on naming them.
    """
    private = ("user_account", "user_profile", "user_credential", "user_job_match",
               "llm_score_cache", "user_query_expansion", "user_session")
    queries = {
        "documents": archive_q._DOCUMENTS,
        "jobs": archive_q._JOBS,
        "lifecycle": archive_q._CLOSURES,
        "document span": archive_q._DOCUMENT_SPAN,
        "job span": archive_q._JOB_SPAN,
        "closure span": archive_q._CLOSURE_SPAN,
    }
    for label, sql in queries.items():
        for table in private:
            assert table not in sql, f"the {label} query reads {table}, which holds personal data"


def test_the_export_only_ever_reads():
    """A nightly job with a write in it is a nightly job that can corrupt the thing it backs up."""
    for sql in (archive_q._DOCUMENTS, archive_q._JOBS, archive_q._CLOSURES,
                archive_q._DOCUMENT_SPAN, archive_q._JOB_SPAN, archive_q._CLOSURE_SPAN):
        # Word-bounded: `updated_at` is a column the export legitimately reads.
        found = re.findall(
            r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER)\b", sql, flags=re.IGNORECASE
        )
        assert not found, f"the export issues {found}"


def test_a_day_that_can_still_change_is_never_frozen():
    """`jobs` lags because a posting's description arrives on a later fetch than its listing."""
    assert streams.JOBS.lag_days > 0, (
        "a jobs partition frozen the day it ends archives rows whose detail has not landed"
    )
    assert streams.DOCUMENTS.lag_days == 0, (
        "source_document is insert-only upstream; a finished day cannot change"
    )


def test_partition_paths_are_one_file_per_day_per_stream():
    assert streams.JOBS.path(date(2026, 9, 13)) == "jobs/2026-09-13.parquet"
    assert streams.DOCUMENTS.path(date(2026, 1, 2)) == "documents/2026-01-02.parquet"


def test_an_unknown_stream_name_is_refused_by_name():
    with pytest.raises(ValueError, match="payloads"):
        streams.by_name(["documents", "payloads"])
    assert streams.by_name(["jobs"]) == (streams.JOBS,)
