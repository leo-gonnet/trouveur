"""The canonical job record: the output of normalisation, the input to derivation.

Normalisation produces *structure*; derivation produces *interpretation*. A location arrives here
as the source wrote it and is parsed into a country and a city one stage later. That split is what
makes a parser fix a re-derive rather than a re-crawl, so resist the urge to interpret here.

Nothing downstream of this model may know which source a row came from. If you find yourself
wanting a source-specific field, map it into the shared vocabulary below instead -- the raw
payload is archived, so nothing is lost by leaving a source's private quirks out of this shape.
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

    # Verbatim, unparsed, and a list because one posting can name several places. Greenhouse
    # writes free text like "Remote, Canada; Remote, US"; parsing it belongs in derivation.
    location_raw: list[str] = Field(default_factory=list)
    salary: SalaryQuote | None = None

    # Hints, not facts: a source's own claim, which derivation may confirm, override or ignore.
    remote_hint: bool | None = None
    employment_type_hint: str | None = None
    agency_hint: bool | None = None
    language_hint: str | None = None
    department_hint: str | None = None

    @property
    def content_hash(self) -> bytes:
        """Hash of everything a reranking model reads.

        This is the LLM score cache key, so it answers exactly one question: would the model see
        different text than last time? It is deliberately NOT a cross-source identity -- that is a
        different question with a different answer, and conflating the two is how V1 silently
        dropped every posting that reached it from a second source. See dedup_key().

        CONTENT_HASH_VERSION is part of the input so that adding a field to the hashed set cannot
        leave pre-existing rows holding a stale hash: bumping it invalidates every row at once.
        """
        parts = [
            str(CONTENT_HASH_VERSION),
            normalize_for_hash(self.title),
            normalize_for_hash(self.company),
            normalize_for_hash(" ".join(self.location_raw)),
            normalize_for_hash(self.description),
        ]
        return hashlib.sha256("|".join(parts).encode()).digest()


def dedup_key(title: str, company: str | None, city: str | None) -> bytes:
    """Fingerprint for 'the same role, posted again or posted elsewhere'.

    Only ever written to a marker column. Two rows sharing a key stay two rows: the decision is
    recorded, never applied, so a wrong pass can be re-run instead of being unpickable.
    """
    parts = [normalize_for_hash(title), normalize_for_hash(company), normalize_for_hash(city)]
    return hashlib.sha256("|".join(parts).encode()).digest()
