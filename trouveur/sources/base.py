"""The contract every source implements."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from trouveur.models import CanonicalJob, RawDocument
from trouveur.sources.http import PoliteClient

DocumentSink = Callable[[Sequence[RawDocument]], Awaitable[None]]

# The scope a source with no tenants sweeps under. Reserved, so two adapters cannot spell it
# differently and partition one corpus into two lifecycles.
GLOBAL_SCOPE = "*"


class ScopeResult(BaseModel):
    """What happened to one tenant in a sweep, for the health panel."""

    scope: str
    ok: bool
    documents: int = 0
    error: str | None = None


class SweepOutcome(BaseModel):
    """What a sweep saw, and whether it saw everything."""

    # Scopes whose ENTIRE live set this sweep observed. Empty means nothing may be closed on the
    # strength of this sweep; a delta sweep is successful and never complete.
    closable_scopes: list[str] = Field(default_factory=list)
    partitions_total: int = 0
    partitions_done: int = 0
    partitions_overflowed: int = 0
    documents: int = 0
    scope_results: list[ScopeResult] = Field(default_factory=list)
    expected: int | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.partitions_done == self.partitions_total

    @property
    def complete(self) -> bool:
        return bool(self.closable_scopes)


@runtime_checkable
class Source(Protocol):
    name: str
    requires_detail: bool

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome: ...

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> RawDocument | None: ...


class Normalizer(Protocol):
    """Pure: raw payloads in, a canonical job out. No I/O, no clock, no database."""

    version: int

    def __call__(
        self, listing: dict, detail: dict | None = None, *, external_id: str
    ) -> CanonicalJob | None: ...
