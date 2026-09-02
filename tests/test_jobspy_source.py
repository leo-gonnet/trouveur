"""Mapping tests for the JobSpy wrapper. The package itself is optional and never called here."""

from __future__ import annotations

from decimal import Decimal

from trouveur.models import SalaryPeriod
from trouveur.sources.jobspy_source import _to_job


def record(**kw) -> dict:
    base = {
        "title": "Wirtschaftsingenieur", "job_url": "https://example.invalid/j/1",
        "company": "ACME", "location": "Wien", "is_remote": True,
        "min_amount": 60000, "max_amount": 80000, "interval": "yearly",
        "date_posted": "2026-08-30", "description": "Prozessoptimierung", "id": "abc",
    }
    return {**base, **kw}


def test_maps_a_complete_record():
    job = _to_job(record(), "AT")
    assert job.title == "Wirtschaftsingenieur"
    assert job.location_country == "AT"
    assert job.remote is True
    assert job.salary_min == Decimal(60000)
    assert job.salary_period is SalaryPeriod.YEAR


def test_record_without_title_or_url_is_discarded():
    assert _to_job(record(title=None), "AT") is None
    assert _to_job(record(job_url=None), "AT") is None


def test_pandas_nan_strings_become_none():
    """JobSpy returns a DataFrame, so missing values arrive as the string 'nan'."""
    job = _to_job(record(company="nan", description="NaN"), "DE")
    assert job.company is None
    assert job.description is None


def test_unknown_interval_falls_back_to_unknown_period():
    assert _to_job(record(interval="fortnightly"), "AT").salary_period is SalaryPeriod.UNKNOWN


def test_falls_back_to_url_when_id_missing():
    job = _to_job(record(id=None), "AT")
    assert job.source_native_id == "https://example.invalid/j/1"
