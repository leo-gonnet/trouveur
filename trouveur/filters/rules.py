"""Stage 1: cheap, deterministic filtering.

This stage exists to make the LLM stage small (see AGENTS.md > LLM cost discipline). It is free
and runs on every job; the LLM only ever sees what survives here.

Bias: when a signal is missing or ambiguous, PASS. A false pass costs a fraction of a cent at the
LLM stage; a false reject means the user never sees a job they wanted.
"""

from __future__ import annotations

from decimal import Decimal

from trouveur.models import Job, ProfileData, RuleVerdict, SalaryPeriod, fold

# Applied in addition to the user's own deal-breakers. These are role types that are never a fit
# for an experienced hire and appear constantly in DACH listings.
DEFAULT_EXCLUDES = (
    "praktikum", "praktikant", "werkstudent", "ausbildung", "auszubildende",
    "duales studium", "schulerpraktikum", "trainee-programm", "ferialjob",
)

# Most generous plausible annualisation, so we only reject when a job is clearly below the floor.
# Austrian contracts commonly pay 14 monthly salaries, hence 14 rather than 12.
_ANNUALISE = {
    SalaryPeriod.YEAR: 1,
    SalaryPeriod.MONTH: 14,
    SalaryPeriod.WEEK: 52,
    SalaryPeriod.DAY: 250,
    SalaryPeriod.HOUR: 2000,
}


def evaluate(job: Job, profile: ProfileData) -> tuple[RuleVerdict, str]:
    """Return a verdict and a human-readable reason."""
    # Two haystacks, deliberately different. Deal-breakers must see the company name (that is how
    # staffing agencies are caught), but role relevance must NOT: a tax-clerk vacancy posted by
    # "Dipl.-Wirtschaftsingenieur Peter X" is not a Wirtschaftsingenieur role.
    full_text = fold(" ".join(filter(None, [job.title, job.company, job.description])))
    role_text = fold(" ".join(filter(None, [job.title, job.description])))
    title = fold(job.title)

    for term in DEFAULT_EXCLUDES:
        if term in title:
            return RuleVerdict.REJECT, f"excluded role type: {term}"

    for term in profile.deal_breakers:
        folded = fold(term)
        if folded and folded in full_text:
            return RuleVerdict.REJECT, f"deal-breaker: {term}"

    if job.raw.get("istArbeitnehmerUeberlassung"):
        return RuleVerdict.REJECT, "staffing agency (Arbeitnehmerüberlassung)"

    # Unknown country passes: some sources omit it, and rejecting on absence loses real jobs.
    if profile.countries and job.location_country:
        if job.location_country not in profile.countries:
            return RuleVerdict.REJECT, f"country {job.location_country} not in target list"

    if profile.remote_only and job.remote is False:
        return RuleVerdict.REJECT, "not remote"

    # A remote job is location-independent, so the city gate only applies to on-site roles.
    if profile.cities and job.remote is not True and job.location_city:
        wanted = [fold(c) for c in profile.cities]
        city = fold(job.location_city)
        if not any(w in city or city in w for w in wanted):
            return RuleVerdict.REJECT, f"city {job.location_city} not in target list"

    if profile.keywords:
        wanted = [fold(k) for k in profile.keywords if k.strip()]
        if wanted and not any(k in role_text for k in wanted):
            return RuleVerdict.REJECT, "no profile keyword present"

    below = _below_salary_floor(job, profile.min_salary_eur_year)
    if below is not None:
        return RuleVerdict.REJECT, below

    return RuleVerdict.PASS, "passed rules"


def _below_salary_floor(job: Job, floor: Decimal) -> str | None:
    if not floor or floor <= 0:
        return None
    stated = job.salary_max or job.salary_min
    if stated is None or job.salary_period == SalaryPeriod.UNKNOWN:
        return None  # no usable signal; let it through
    annual = Decimal(stated) * _ANNUALISE[job.salary_period]
    if annual < floor:
        return f"salary ~{int(annual)} EUR/yr below floor {int(floor)}"
    return None


def title_is_plausible(title: str, profile: ProfileData) -> bool:
    """Cheap title-only gate, used before spending a request on a detail page.

    Sources that render titles in their listings (karriere.at) can reject obvious misses for
    free. Intentionally more permissive than evaluate(): it only sees a title, so anything it
    lets through is still filtered properly later.
    """
    folded = fold(title)
    if any(term in folded for term in DEFAULT_EXCLUDES):
        return False
    for term in profile.deal_breakers:
        breaker = fold(term)
        if breaker and breaker in folded:
            return False
    return True
