"""The embedding provider seam.

Local today, an API tomorrow, without touching anything that stores or queries a vector. A
provider is responsible for one thing: turning text into vectors of a fixed width, the same way
every time.

Two rules the protocol exists to enforce:

Embedding is asymmetric. Retrieval models are trained with different prefixes for documents and
for queries, and embedding both the same way costs real recall while looking like it works. Hence
two methods rather than one with a flag.

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

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_queries(self, texts: list[str]) -> list[list[float]]: ...


def version_of(provider: EmbeddingProvider) -> str:
    return f"{provider.name}:{provider.model}:{provider.dim}"
