from __future__ import annotations

from decimal import Decimal

import pytest

from trouveur.filters.rules import evaluate
from trouveur.models import Job, ProfileData, RuleVerdict, SalaryPeriod


def job(**kw) -> Job:
    base = dict(source="arbeitsagentur", source_native_id="1", url="u",
                title="Wirtschaftsingenieur (m/w/d)")
    return Job(**{**base, **kw})


def profile(**kw) -> ProfileData:
    base = dict(keywords=["Wirtschaftsingenieur"], countries=["AT", "DE", "CH"])
    return ProfileData(**{**base, **kw})


def verdict(j: Job, p: ProfileData) -> RuleVerdict:
    return evaluate(j, p)[0]


def test_passes_a_matching_wien_remote_role():
    j = job(location_city="Wien", location_country="AT", remote=True)
    assert verdict(j, profile()) is RuleVerdict.PASS


@pytest.mark.parametrize(
    "title",
    ["Werkstudent (m/w/d) Logistik", "Praktikum Supply Chain", "Ausbildung zum Industriekaufmann"],
)
def test_rejects_junior_role_types(title):
    assert verdict(job(title=title), profile(keywords=[])) is RuleVerdict.REJECT


def test_rejects_when_country_outside_target_list():
    j = job(location_city="Paris", location_country="FR")
    assert verdict(j, profile()) is RuleVerdict.REJECT


def test_passes_when_country_unknown():
    """Some sources omit country. Rejecting on absence loses real jobs."""
    assert verdict(job(location_country=None), profile()) is RuleVerdict.PASS


def test_rejects_onsite_role_outside_target_cities():
    j = job(location_city="Hamburg", location_country="DE", remote=False)
    assert verdict(j, profile(cities=["Wien"])) is RuleVerdict.REJECT


def test_city_gate_does_not_apply_to_remote_roles():
    j = job(location_city="Hamburg", location_country="DE", remote=True)
    assert verdict(j, profile(cities=["Wien"])) is RuleVerdict.PASS


def test_rejects_non_remote_when_remote_only():
    j = job(location_city="Wien", location_country="AT", remote=False)
    assert verdict(j, profile(remote_only=True)) is RuleVerdict.REJECT


def test_rejects_staffing_agency_flag_from_enrichment():
    j = job(location_country="DE", raw={"istArbeitnehmerUeberlassung": True})
    assert verdict(j, profile()) is RuleVerdict.REJECT


def test_deal_breaker_matches_company_name():
    """Deal-breakers must see the company: that is how agencies are named."""
    j = job(company="AGILIOS Personal GmbH", location_country="DE")
    assert verdict(j, profile(deal_breakers=["Personal GmbH"])) is RuleVerdict.REJECT


def test_relevance_keyword_must_not_match_company_name():
    """Regression: a tax-clerk vacancy posted by 'Dipl.-Wirtschaftsingenieur Peter X' passed the
    keyword gate because the company name contained the keyword."""
    j = job(title="Steuerfachangestellte(n), Bilanzbuchhalter (m/w/d)",
            company="Dipl.-Wirtschaftsingenieur Peter Beispiel", location_country="DE")
    assert verdict(j, profile()) is RuleVerdict.REJECT


def test_rejects_when_salary_clearly_below_floor():
    j = job(location_country="DE", salary_max=Decimal(30000), salary_period=SalaryPeriod.YEAR)
    assert verdict(j, profile(min_salary_eur_year=Decimal(55000))) is RuleVerdict.REJECT


def test_passes_when_salary_unstated():
    j = job(location_country="DE")
    assert verdict(j, profile(min_salary_eur_year=Decimal(55000))) is RuleVerdict.PASS


def test_monthly_austrian_salary_annualises_over_fourteen_months():
    """4500/month x 14 = 63000, which clears a 55k floor. Using 12 would wrongly reject it."""
    j = job(location_country="AT", salary_max=Decimal(4500), salary_period=SalaryPeriod.MONTH)
    assert verdict(j, profile(min_salary_eur_year=Decimal(55000))) is RuleVerdict.PASS
