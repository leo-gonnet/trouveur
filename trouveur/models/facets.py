"""Derived facets: the interpreted view of a job, and the only one anything downstream may read.

Two rules govern everything in this module.

Never guess. Every enum has an UNKNOWN member and every scalar is nullable, because a wrong facet
is worse than a missing one: it silently poisons filters and rankings while looking like data. A
posting that does not state its seniority gets UNKNOWN, not a guess from the title.

Never re-derive downstream. If the matcher or the web layer parses a location or extracts a skill
again, there are two answers to one question and they will diverge -- which surfaces as a filter
and a score disagreeing about the same posting, and is close to undebuggable from the symptom. If
something is missing here, add it here.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class WorkMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    EXECUTIVE = "executive"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    APPRENTICESHIP = "apprenticeship"
    UNKNOWN = "unknown"


class JobFacets(BaseModel):
    # Lists, because one posting can legitimately name several places, and collapsing
    # "Remote, Canada; Remote, US" to a single country would silently drop half of what a
    # country filter is supposed to match against.
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)

    work_mode: WorkMode = WorkMode.UNKNOWN
    seniority: Seniority = Seniority.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN

    # Always EUR per year, whatever the source quoted, so one filter serves every source. None
    # means the posting stated nothing usable -- never a zero, which would read as "unpaid".
    salary_min_eur_year: Decimal | None = None
    salary_max_eur_year: Decimal | None = None
    # True when the figure came from annualising a shorter period rather than a stated annual sum.
    salary_annualised: bool = False

    language: str | None = None
    is_agency: bool | None = None
    skills: list[str] = Field(default_factory=list)
