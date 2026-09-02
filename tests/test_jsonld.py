from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from trouveur.models import SalaryPeriod
from trouveur.sources import jsonld

FIXTURES = Path(__file__).parent / "fixtures"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parses_a_standard_job_posting():
    posting = jsonld.find_job_posting(_html("jobposting.html"))
    job = jsonld.to_job(posting, source="karriere_at", url="https://example.invalid/jobs/1")
    assert job.title == "Wirtschaftsingenieur (m/w/d) Prozessoptimierung"
    assert job.company == "Beispiel GmbH"
    assert job.location_city == "Wien"
    assert job.location_country == "AT"
    assert job.source_native_id == "10031251"


def test_parses_monthly_salary_range():
    """Austrian postings state salary monthly; the period must survive into the model."""
    posting = jsonld.find_job_posting(_html("jobposting.html"))
    job = jsonld.to_job(posting, source="karriere_at", url="u")
    assert job.salary_min == Decimal(3500)
    assert job.salary_max == Decimal(4500)
    assert job.salary_period is SalaryPeriod.MONTH


def test_strips_html_from_description():
    posting = jsonld.find_job_posting(_html("jobposting.html"))
    job = jsonld.to_job(posting, source="karriere_at", url="u")
    assert "<b>" not in job.description
    assert "Führungskraft" in job.description


def test_finds_posting_inside_an_at_graph():
    posting = jsonld.find_job_posting(_html("jobposting_graph.html"))
    assert posting is not None
    job = jsonld.to_job(posting, source="x", url="u")
    assert job.title == "Remote Industrial Engineer"


def test_telecommute_maps_to_remote():
    posting = jsonld.find_job_posting(_html("jobposting_graph.html"))
    job = jsonld.to_job(posting, source="x", url="u")
    assert job.remote is True


def test_exact_salary_value_populates_both_bounds():
    posting = jsonld.find_job_posting(_html("jobposting_graph.html"))
    job = jsonld.to_job(posting, source="x", url="u")
    assert job.salary_min == job.salary_max == Decimal(72000)
    assert job.salary_period is SalaryPeriod.YEAR


def test_returns_none_when_document_has_no_job_posting():
    assert jsonld.find_job_posting("<html><body>nothing here</body></html>") is None


def test_malformed_json_ld_is_skipped_not_raised():
    html = '<script type="application/ld+json">{not json at all</script>'
    assert jsonld.extract_blocks(html) == []


def test_posting_without_a_title_is_discarded():
    assert jsonld.to_job({"@type": "JobPosting"}, source="x", url="u") is None


def test_falls_back_to_url_when_no_identifier():
    job = jsonld.to_job({"title": "Engineer"}, source="x", url="https://example.invalid/j/9")
    assert job.source_native_id == "https://example.invalid/j/9"
