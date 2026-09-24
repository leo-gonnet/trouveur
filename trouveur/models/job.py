"""The canonical job record: the output of normalisation, the input to derivation.

Normalisation produces STRUCTURE; derivation produces INTERPRETATION. That split is what makes a
parser fix a re-derive rather than a re-crawl, so resist the urge to interpret here. Nothing
downstream may know which source a row came from.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from trouveur.models.text import normalize_for_hash
from trouveur.versions import CONTENT_HASH_VERSION


class SalaryPeriod(StrEnum):
    YEAR = "YEAR"
    MONTH = "MONTH"
    WEEK = "WEEK"
    DAY = "DAY"
    HOUR = "HOUR"
    UNKNOWN = "UNKNOWN"


class Location(BaseModel):
    """One place a posting names.

    `raw` is the source's own wording and is never discarded. The structured fields are filled
    only where the source stated them separately; derivation parses `raw` where they are absent.
    """

    raw: str
    city: str | None = None
    region: str | None = None
    # Verbatim as the source wrote it ("DEUTSCHLAND", "Italy"). Mapping to ISO is derivation.
    country: str | None = None


class SalaryQuote(BaseModel):
    """A salary exactly as the source stated it. Annualisation happens in derivation."""

    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    currency: str | None = None
    period: SalaryPeriod = SalaryPeriod.UNKNOWN


class CanonicalJob(BaseModel):
    source: str
    external_id: str
    url: str
    title: str
    company: str | None = None
    description: str | None = None

    posted_at: datetime | None = None
    updated_at: datetime | None = None
    closes_at: datetime | None = None

    locations: list[Location] = Field(default_factory=list)
    salary: SalaryQuote | None = None

    # Hints, not facts: a source's own claim, which derivation may confirm, override or ignore.
    remote_hint: bool | None = None
    employment_type_hint: str | None = None
    agency_hint: bool | None = None
    language_hint: str | None = None
    department_hint: str | None = None

    @property
    def content_hash(self) -> bytes:
        """Hash of everything a reranking model reads: the LLM score cache key.

        NOT a cross-source identity -- that is dedup_key(), a different question. The version is
        part of the input, so adding a hashed field cannot leave old rows holding a stale hash.
        """
        parts = [
            str(CONTENT_HASH_VERSION),
            normalize_for_hash(self.title),
            normalize_for_hash(self.company),
            normalize_for_hash(" ".join(loc.raw for loc in self.locations)),
            normalize_for_hash(self.description),
        ]
        return hashlib.sha256("|".join(parts).encode()).digest()


def dedup_key(title: str, company: str | None, city: str | None) -> bytes:
    """Fingerprint for 'the same role, posted again or posted elsewhere'.

    Only ever written to a MARKER column: two rows sharing a key stay two rows, so a wrong pass
    can be re-run instead of being unpickable.
    """
    parts = [normalize_for_hash(title), normalize_for_hash(company), normalize_for_hash(city)]
    return hashlib.sha256("|".join(parts).encode()).digest()
