from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from trouveur.models import Job
from trouveur.sources.http import PoliteClient


@runtime_checkable
class Source(Protocol):
    """A source yields jobs. It never writes to the database (see AGENTS.md)."""

    name: str

    def fetch(self, client: PoliteClient, since: datetime | None) -> AsyncIterator[Job]: ...


@runtime_checkable
class Enrichable(Protocol):
    """Optional: fill in fields too expensive to fetch during the initial sweep.

    Arbeitsagentur descriptions cost one request each, so they are fetched only for postings that
    survive the rules filter.
    """

    name: str

    async def enrich(self, client: PoliteClient, native_id: str) -> dict: ...
