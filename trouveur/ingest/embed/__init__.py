"""Provider selection.

One provider per process, chosen by configuration. Callers ask for the version string rather than
hardcoding one, so switching providers changes a setting and then refills the embed queue --
which re-embeds the corpus through the ordinary worker rather than through a bespoke script.
"""

from __future__ import annotations

import logging
from functools import cache

from trouveur.config import get_settings
from trouveur.ingest.embed.base import EMBEDDING_DIM, EmbeddingProvider, version_of
from trouveur.ingest.embed.deterministic import DeterministicProvider
from trouveur.ingest.embed.local import LocalOnnxMpnetProvider, LocalOnnxProvider

log = logging.getLogger(__name__)

_PROVIDERS = {
    "local-onnx": LocalOnnxProvider,
    "local-onnx-mpnet": LocalOnnxMpnetProvider,
    "deterministic": DeterministicProvider,
}


@cache
def _build(name: str, expected_dim: int, setting: str) -> EmbeddingProvider:
    try:
        factory = _PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS))
        raise RuntimeError(
            f"Unknown embedding provider {name!r}; known providers are: {known}."
        ) from None
    provider = factory()
    if provider.dim != expected_dim:
        raise RuntimeError(
            f"Provider {name!r} produces {provider.dim} dimensions but {setting} is "
            f"{expected_dim}; changing width is a migration paired with that setting, not a "
            "setting edit on its own."
        )
    return provider


def get_provider() -> EmbeddingProvider:
    """The provider the embed worker WRITES with."""
    settings = get_settings()
    return _build(settings.embedding_provider, settings.embedding_dim, "EMBEDDING_DIM")


def get_query_provider() -> EmbeddingProvider:
    """The provider retrieval embeds its QUERIES with, which must match the space it reads.

    A query vector is compared against a stored one, so it has to come from the same model -- not
    merely the same width. Reading the narrow column with a query the wide model produced is not a
    subtle degradation: pgvector refuses it outright with "different halfvec dimensions 768 and
    384", so every recommendation fails.

    That is exactly the state a model swap spends its whole backfill in. EMBEDDING_DIM and
    EMBEDDING_READ_DIM exist so the worker can fill the new space while the dense arm keeps
    serving the old one, but a width alone cannot say which model wrote that space. This is the
    other half of that pair: leave it unset and reads follow writes, which is every day that is
    not a migration; set it to the outgoing provider for the length of the backfill.
    """
    settings = get_settings()
    name = settings.embedding_read_provider or settings.embedding_provider
    return _build(name, settings.embedding_read_dim, "EMBEDDING_READ_DIM")


def embedding_version() -> str:
    return version_of(get_provider())


__all__ = [
    "EMBEDDING_DIM",
    "DeterministicProvider",
    "LocalOnnxMpnetProvider",
    "EmbeddingProvider",
    "LocalOnnxProvider",
    "embedding_version",
    "get_provider",
    "get_query_provider",
    "version_of",
]
