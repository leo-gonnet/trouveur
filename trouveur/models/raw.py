"""The raw archive: source payloads stored verbatim, kept forever -- the one input that cannot
be recomputed, and what makes a normaliser fix a re-derive rather than a re-crawl."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DocumentKind(StrEnum):
    """Why a payload was fetched. Archived separately, so a failed detail fetch cannot blank a
    description we already hold."""

    LISTING = "listing"
    DETAIL = "detail"


class RawDocument(BaseModel):
    source: str
    external_id: str
    kind: DocumentKind
    # The sub-population this document belongs to. Lifecycle closes WITHIN a scope, so one
    # tenant's complete dump cannot retire a tenant whose board failed to answer.
    scope: str | None = None
    payload: dict[str, Any]
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def payload_sha256(self) -> bytes:
        """Identity of the bytes, so re-fetching unchanged content archives nothing new.

        sort_keys is load-bearing: without it an unchanged posting hashes differently on every
        fetch and archives a duplicate each day.
        """
        canonical = json.dumps(self.payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).digest()
