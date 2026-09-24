"""Derived facets: the interpreted view of a job, and the only one anything downstream may read.

Never guess -- every enum has UNKNOWN and every scalar is nullable, because a wrong facet poisons
every filter that reads it while looking like data. Never re-derive downstream: if something is
missing here, add it here.
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
    # Lists: collapsing "Remote, Canada; Remote, US" to one country would drop half of what a
    # country filter should match.
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)

    work_mode: WorkMode = WorkMode.UNKNOWN
    seniority: Seniority = Seniority.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN

    # Always EUR per year, whatever the source quoted. None means nothing usable was stated --
    # never a zero, which reads as "unpaid".
    salary_min_eur_year: Decimal | None = None
    salary_max_eur_year: Decimal | None = None
    salary_annualised: bool = False

    language: str | None = None
    is_agency: bool | None = None
    skills: list[str] = Field(default_factory=list)
