"""Per-user search profile and credentials.

Profiles are per user and carry their own version. Editing one deliberately invalidates that
user's cached LLM scores and nobody else's -- which is why version lives here rather than in a
singleton table, and why a company registry is a separate table (adding a company must not bump a
profile version and bill every user for a re-score).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from trouveur.models.facets import EmploymentType, Seniority, WorkMode


class UserProfile(BaseModel):
    user_id: int
    version: int = 1

    title: str = ""
    years_experience: int = 0
    objectives: str = ""
    languages: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    deal_breakers: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    countries: list[str] = Field(default_factory=lambda: ["AT", "DE", "CH"])
    cities: list[str] = Field(default_factory=list)
    work_modes: list[WorkMode] = Field(default_factory=list)
    seniorities: list[Seniority] = Field(default_factory=list)
    employment_types: list[EmploymentType] = Field(default_factory=list)
    min_salary_eur_year: Decimal = Decimal(0)

    retrieval_limit: int = 400
    rerank_limit: int = 150
    notify_threshold: int = 70
