"""Guards for deterministic derivation.

Most of these encode a wrong answer that derivation used to give confidently. A bad facet never
raises: it silently poisons every filter and ranking that reads it, so each of these is a bug
that would otherwise be found by a user wondering why a job never appeared.
"""

from __future__ import annotations

from decimal import Decimal

from trouveur.ingest.derive import derive
from trouveur.models import (
    CanonicalJob,
    EmploymentType,
    Location,
    SalaryPeriod,
    SalaryQuote,
    Seniority,
    WorkMode,
)
from trouveur.sources.arbeitsagentur import normalize as aa_normalize
from trouveur.sources.greenhouse import normalize as gh_normalize


def _job(**kwargs) -> CanonicalJob:
    return CanonicalJob(
        source="test", external_id="1", url="https://example.test", **kwargs
    )


def test_structured_address_is_used_without_reparsing(aa_listing):
    facets = derive(aa_normalize(aa_listing))
    assert facets.countries == ["DE"]
    assert facets.regions == ["Bayern"]
    # The disambiguating suffix Arbeitsagentur appends is dropped, the city is not.
    assert facets.cities == ["München"]


def test_free_text_location_is_parsed_only_where_structure_is_absent():
    facets = derive(_job(title="Engineer", locations=[Location(raw="Bangalore, India")]))
    assert facets.countries == ["IN"]
    assert facets.cities == ["Bangalore"]


def test_every_named_country_survives_a_multi_location_posting(gh_board):
    job = gh_normalize(gh_board["jobs"][0], external_id="beispiel:4001")
    facets = derive(job)
    # Collapsing these to one value would silently drop half of what a country filter matches on.
    assert set(facets.countries) == {"DE", "AT"}
    assert facets.work_mode is WorkMode.REMOTE


def test_unknown_country_derives_to_nothing_rather_than_a_guess():
    facets = derive(_job(title="Engineer", locations=[Location(raw="Atlantis")]))
    assert facets.countries == []


def test_benefits_prose_does_not_decide_work_mode(gh_board):
    """A benefits list is not a work-mode statement.

    "flexible paid time off" previously matched a "flexible" hybrid term and classified an
    on-site Austrian role as hybrid.
    """
    job = gh_normalize(gh_board["jobs"][1], external_id="beispiel:4002")
    assert "flexible paid time off" in job.description
    facets = derive(job)
    assert facets.work_mode is not WorkMode.HYBRID


def test_company_boilerplate_does_not_decide_work_mode():
    """An employer describing itself is not this vacancy describing itself.

    A description saying "we are an all-remote company" previously marked an explicitly
    Bangalore-based on-site role as remote. Unknown is the honest answer.
    """
    facets = derive(
        _job(
            title="Process Engineer",
            description="We are an all-remote company with a remote-first culture.",
            locations=[Location(raw="Bangalore, India")],
        )
    )
    assert facets.work_mode is WorkMode.UNKNOWN


def test_structured_remote_hint_outranks_everything():
    assert derive(_job(title="Engineer", remote_hint=True)).work_mode is WorkMode.REMOTE
    assert derive(_job(title="Engineer", remote_hint=False)).work_mode is WorkMode.ONSITE


def test_seniority_reads_the_title_not_the_description():
    # Descriptions mention seniority constantly in requirements, which classifies the wrong thing.
    assert derive(_job(title="Senior Engineer")).seniority is Seniority.SENIOR
    assert (
        derive(
            _job(title="Engineer", description="You will mentor our senior engineers.")
        ).seniority
        is Seniority.UNKNOWN
    )


def test_leadership_titles_outrank_plain_seniority():
    assert derive(_job(title="Head of Operations")).seniority is Seniority.LEAD
    assert derive(_job(title="Director of Engineering")).seniority is Seniority.EXECUTIVE


def test_austrian_monthly_salary_annualises_over_fourteen_months():
    """Austrian contracts commonly pay 14 monthly salaries; German ones do not.

    Annualising generously matters because this only decides whether a posting clears a floor:
    over-estimating lets a borderline job be judged on its merits, under-estimating hides it.
    """
    salary = SalaryQuote(amount_min=Decimal(4000), currency="EUR", period=SalaryPeriod.MONTH)
    def _at(raw: str):
        return derive(_job(title="Engineer", salary=salary, locations=[Location(raw=raw)]))

    austrian = _at("Wien, Austria")
    german = _at("Berlin, Germany")
    assert austrian.salary_min_eur_year == Decimal(56000)
    assert german.salary_min_eur_year == Decimal(48000)
    assert austrian.salary_annualised is True


def test_annual_salary_is_not_marked_annualised(aa_listing):
    facets = derive(aa_normalize(aa_listing))
    assert facets.salary_min_eur_year == Decimal(62000)
    assert facets.salary_annualised is False


def test_non_euro_salary_is_not_converted():
    # Converting would need an exchange rate, and a stale rate is a wrong number that looks right.
    facets = derive(
        _job(
            title="Engineer",
            salary=SalaryQuote(amount_min=Decimal(90000), currency="USD", period=SalaryPeriod.YEAR),
        )
    )
    assert facets.salary_min_eur_year is None


def test_missing_salary_is_none_not_zero():
    # Zero would read as "unpaid" to a salary filter rather than "unstated".
    assert derive(_job(title="Engineer")).salary_min_eur_year is None


def test_skills_come_from_the_dictionary_only(aa_listing, aa_detail):
    facets = derive(aa_normalize(aa_listing, aa_detail))
    assert "lean management" in facets.skills
    assert "six sigma" in facets.skills
    assert "sap" in facets.skills


def test_employment_hint_outranks_title_matching():
    assert (
        derive(_job(title="Engineer", employment_type_hint="teilzeit")).employment_type
        is EmploymentType.PART_TIME
    )
    assert (
        derive(_job(title="Praktikum Logistik")).employment_type is EmploymentType.INTERNSHIP
    )
