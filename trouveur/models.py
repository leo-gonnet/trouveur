"""Shared domain models.

`Job` is the single contract between source adapters, the database layer and the web views.
Do not define a parallel dict shape for the same thing (see AGENTS.md).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class SalaryPeriod(StrEnum):
    YEAR = "YEAR"
    MONTH = "MONTH"
    WEEK = "WEEK"
    DAY = "DAY"
    HOUR = "HOUR"
    UNKNOWN = "UNKNOWN"


class UserState(StrEnum):
    NEW = "new"
    INTERESTED = "interested"
    APPLIED = "applied"
    REJECTED = "rejected"


class RuleVerdict(StrEnum):
    PASS = "pass"
    REJECT = "reject"
    UNKNOWN = "unknown"


_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def fold(text: str) -> str:
    """Lowercase and strip diacritics: 'München' -> 'munchen'.

    Used for the trigram search column and for dedupe hashing. Postgres does the same folding
    with unaccent(); this must stay behaviourally identical to it.
    """
    lowered = text.lower().replace("ß", "ss")
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_for_hash(text: str | None) -> str:
    if not text:
        return ""
    return _SPACE.sub(" ", _PUNCT.sub(" ", fold(text))).strip()


class Job(BaseModel):
    source: str
    source_native_id: str
    url: str
    title: str
    company: str | None = None
    location_city: str | None = None
    location_country: str | None = None
    remote: bool | None = None
    salary_min: Decimal | None = None
    salary_max: Decimal | None = None
    salary_period: SalaryPeriod = SalaryPeriod.UNKNOWN
    posted_at: datetime | None = None
    description: str | None = None
    raw: dict = Field(default_factory=dict)

    @property
    def content_hash(self) -> bytes:
        """Identity across sources.

        The same posting reaches us from Arbeitsagentur, Indeed and karriere.at; collapsing them
        is load-bearing, not a nicety. Deliberately excludes url, salary and description, which
        differ between aggregators for one underlying vacancy.
        """
        parts = [
            normalize_for_hash(self.title),
            normalize_for_hash(self.company),
            normalize_for_hash(self.location_city),
        ]
        return hashlib.sha256("|".join(parts).encode()).digest()

    @property
    def search_norm(self) -> str:
        return fold(" ".join(filter(None, [self.title, self.company, self.description or ""])))


class ProfileData(BaseModel):
    """The user's profile. Stored in the database, edited in the web UI, never committed."""

    version: int = 1
    title: str = ""
    years_experience: int = 0
    languages: list[str] = Field(default_factory=list)
    objectives: str = ""
    must_have: list[str] = Field(default_factory=list)
    deal_breakers: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=lambda: ["AT", "DE", "CH"])
    remote_only: bool = False
    min_salary_eur_year: Decimal = Decimal(0)
    notify_threshold: int = 70
