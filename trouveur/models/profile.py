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

# What a profile may say, by code. Countries are exactly the codes derivation can produce from
# ingest/vocab.py (a test pins the two together), because a country the form offers but no
# posting can ever derive is a filter that silently matches nothing. Languages go to the reranker
# only, so the list is what candidates on a DACH board plausibly speak.
COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria", "DE": "Germany", "CH": "Switzerland", "LU": "Luxembourg",
    "NL": "Netherlands", "BE": "Belgium", "FR": "France", "IT": "Italy", "ES": "Spain",
    "PT": "Portugal", "PL": "Poland", "CZ": "Czechia", "SK": "Slovakia", "SI": "Slovenia",
    "HU": "Hungary", "HR": "Croatia", "RO": "Romania", "BG": "Bulgaria", "DK": "Denmark",
    "SE": "Sweden", "NO": "Norway", "FI": "Finland", "IE": "Ireland", "GB": "United Kingdom",
    "GR": "Greece", "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania", "US": "United States",
    "CA": "Canada", "IN": "India", "IL": "Israel", "SG": "Singapore", "AU": "Australia",
    "JP": "Japan", "BR": "Brazil", "TR": "Türkiye", "ZA": "South Africa",
}

LANGUAGES: dict[str, str] = {
    "de": "German", "en": "English", "fr": "French", "it": "Italian", "es": "Spanish",
    "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "cs": "Czech", "sk": "Slovak",
    "hu": "Hungarian", "ro": "Romanian", "hr": "Croatian", "sl": "Slovenian", "tr": "Turkish",
    "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic", "zh": "Chinese", "ja": "Japanese",
}


# How much background text the form accepts. Roughly a dense page -- enough for a career in
# summary, short of a pasted CV. Measured against the prompts that read it: the expansion prompt
# sees it whole once per profile version, and the reranker sees a distillation of it.
BACKGROUND_MAX_CHARS = 4000


class UserProfile(BaseModel):
    user_id: int
    version: int = 1

    title: str = ""
    years_experience: int = 0
    objectives: str = ""
    # What the candidate has done, as a CV summary in their own words. Capped because both
    # prompts that read it have a finite attention budget: a CV pasted wholesale buries the
    # objectives it is meant to support. The cap is enforced by the form, not here, so an
    # over-long row already in the database still loads.
    background: str = ""
    languages: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    countries: list[str] = Field(default_factory=lambda: ["AT", "DE", "CH"])
    cities: list[str] = Field(default_factory=list)
    work_modes: list[WorkMode] = Field(default_factory=list)
    seniorities: list[Seniority] = Field(default_factory=list)
    employment_types: list[EmploymentType] = Field(default_factory=list)
    min_salary_eur_year: Decimal = Decimal(0)

    # The user's pause on spending. Off, no paid call runs for them at all; retrieval is free
    # and keeps going, so Search still works. Never a SCORING_FIELD: it decides whether we spend,
    # not what a good match is, so toggling it must not invalidate a cached score.
    scoring_enabled: bool = True
