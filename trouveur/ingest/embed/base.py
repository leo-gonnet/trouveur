"""The embedding provider seam.

Documents and queries have separate methods because whether they need different prefixes is a
property of the model, and either mistake is silent -- so the provider owns them, not the caller.
The version is a string naming provider, model and width: an integer could not express that two
rows came from different vector spaces, and mixing spaces returns nonsense rather than an error.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Fixed by the column type. Changing it is a migration, not a config edit.
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


# Width -> the column holding that space. Named once: the writer and the reader disagreeing is a
# silent wrong-neighbours bug, not an error.
_COLUMNS = {384: "embedding", 768: "embedding_768"}


def column_for(dim: int) -> str:
    try:
        return _COLUMNS[dim]
    except KeyError:
        raise RuntimeError(
            f"No embedding column is {dim} wide; known widths are "
            f"{', '.join(str(d) for d in sorted(_COLUMNS))}. Adding one is a migration."
        ) from None
