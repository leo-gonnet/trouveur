"""The contract every source implements.

A source fetches and hands raw payloads to the pipeline; it never writes, never normalises and
never interprets. Parsing lives in the source's own normalize module so that it can be tested
against a golden fixture with no network and no database.

Documents are handed over in batches rather than one at a time. At tens of thousands of documents
per sweep the round trip, not the work, is the bottleneck, and a per-document sink invites callers
to write per-document SQL.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from trouveur.models import CanonicalJob, RawDocument
from trouveur.sources.http import PoliteClient

DocumentSink = Callable[[Sequence[RawDocument]], Awaitable[None]]


class ScopeResult(BaseModel):
    """What happened to one tenant in a sweep, for the health panel.

    Separate from `closable_scopes` because they answer different questions: this records whether
    the request succeeded, that records whether the response justified retiring postings. They
    coincide for a complete per-tenant dump and would not for a paginated one.
    """

    scope: str
    ok: bool
    documents: int = 0
    error: str | None = None


class SweepOutcome(BaseModel):
    """What a sweep saw, and -- critically -- whether it saw everything.

    Completeness is not success. It answers one question: did this sweep observe a source's entire
    live set, such that a posting we hold and did not see can be presumed gone?

    A delta sweep is a successful sweep and is never complete: it only ever returns postings
    published inside its window, so it says nothing about whether an older posting is still live.
    Closing on one would retire the whole corpus on the first run. Completeness is also per scope
    rather than per source, because one tenant's board failing says nothing about another's.
    """

    # Scopes whose ENTIRE live set this sweep observed, so a posting we hold within one and did
    # not see may be presumed gone. A scope is a source-defined prefix -- for a per-tenant board
    # it is the tenant. Empty means nothing may be closed on the strength of this sweep.
    closable_scopes: list[str] = Field(default_factory=list)
    partitions_total: int = 0
    partitions_done: int = 0
    partitions_overflowed: int = 0
    documents: int = 0
    # Per tenant, for the health panel. Empty for a source that has no tenants.
    scope_results: list[ScopeResult] = Field(default_factory=list)
    # What the source claimed was available, when it says. A shortfall against `documents` is a
    # measured coverage hole rather than an assumption that there wasn't one.
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
    # Whether a listing alone is enough. When true the pipeline queues a detail fetch per posting
    # and drains it continuously at a polite rate, rather than making the sweep pay for it inline.
    requires_detail: bool

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome: ...

    async def fetch_detail(
        self, client: PoliteClient, external_id: str
    ) -> RawDocument | None: ...


class Normalizer(Protocol):
    """Pure: raw payloads in, a canonical job out. No I/O, no clock, no database.

    Purity is what makes re-derivation possible. If this function reaches for the network or for
    now(), replaying it over the archive stops reproducing the original result.
    """

    version: int

    def __call__(
        self, listing: dict, detail: dict | None = None, *, external_id: str
    ) -> CanonicalJob | None: ...
