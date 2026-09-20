"""Provider selection.

One provider per process, chosen by configuration. Callers ask for the version string rather than
hardcoding one, so switching providers changes a setting and then refills the embed queue --
which re-embeds the corpus through the ordinary worker rather than through a bespoke script.
"""

from __future__ import annotations

import logging
from functools import lru_cache

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


@lru_cache(maxsize=1)
def get_provider() -> EmbeddingProvider:
    name = get_settings().embedding_provider
    try:
        factory = _PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS))
        raise RuntimeError(
            f"Unknown embedding provider {name!r}; known providers are: {known}."
        ) from None
    provider = factory()
    if provider.dim != get_settings().embedding_dim:
        raise RuntimeError(
            f"Provider {name!r} produces {provider.dim} dimensions but the embedding column is "
            f"halfvec({get_settings().embedding_dim}); changing width is a migration paired with "
            "EMBEDDING_DIM, not a setting edit on its own."
        )
    return provider


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
    "version_of",
]
