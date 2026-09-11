"""The raw archive: source payloads stored verbatim, kept forever.

Everything else in the system is a pure function of these rows, so they are the one thing that
cannot be recomputed. A normaliser bug is repaired by re-deriving from here; if the payload were
not kept, the only repair would be re-fetching a million postings from the source.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DocumentKind(StrEnum):
    """Why a payload was fetched. Kinds are archived separately on purpose.

    A source's listing and detail responses have different freshness and different failure modes.
    Keeping them apart means a failed detail fetch cannot blank a description we already hold.
    """

    LISTING = "listing"
    DETAIL = "detail"


class RawDocument(BaseModel):
    source: str
    external_id: str
    kind: DocumentKind
    # The sub-population this document belongs to, when the source has one -- a Greenhouse board,
    # say. Lifecycle closes within a scope, so this is what lets one tenant's complete dump retire
    # its own stale postings without touching a tenant whose board failed to answer.
    scope: str | None = None
    payload: dict[str, Any]
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def payload_sha256(self) -> bytes:
        """Identity of the bytes, so re-fetching unchanged content archives nothing new.

        sort_keys is load-bearing: dict ordering is not stable across payloads, and without it an
        unchanged posting would hash differently on every fetch and archive a duplicate each day.
        """
        canonical = json.dumps(self.payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).digest()
