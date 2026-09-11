"""The embedding provider seam.

Local today, an API tomorrow, without touching anything that stores or queries a vector. A
provider is responsible for one thing: turning text into vectors of a fixed width, the same way
every time.

Two rules the protocol exists to enforce:

Documents and queries are embedded through separate methods, because whether they need different
treatment is a property of the model. e5-family models expect "passage: " and "query: " prefixes
and lose real recall without them; sentence-similarity models are symmetric and take none. Either
mistake is silent, so the provider owns the prefixes and no caller can forget or misapply them.

The version is a string, never an integer. It names the provider, the model and the width, so a
row's vector space is legible from the row. An integer could not express that two rows came from
different spaces, and mixing spaces in one column returns confident nonsense rather than an error.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Fixed by the column type: halfvec(384). Changing it is a migration, not a config edit, so a
# provider whose width differs must be rejected loudly rather than truncated to fit.
EMBEDDING_DIM = 384


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    model: str
    dim: int
    document_prefix: str
    query_prefix: str

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_queries(self, texts: list[str]) -> list[list[float]]: ...


def version_of(provider: EmbeddingProvider) -> str:
    return f"{provider.name}:{provider.model}:{provider.dim}"
