"""Local ONNX embeddings. No network, no per-document cost, no torch.

A model swap must set the prefixes alongside the name: this model is symmetric and takes none,
the e5 family expects "passage: "/"query: " and loses recall without them, and either mistake is
silent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trouveur.ingest.embed.base import EMBEDDING_DIM

log = logging.getLogger(__name__)

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DOCUMENT_PREFIX = ""
QUERY_PREFIX = ""
_BATCH = 64


class LocalOnnxProvider:
    name = "local-onnx"
    model = MODEL
    dim = EMBEDDING_DIM

    def __init__(self) -> None:
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise RuntimeError(
                    "The local embedding provider needs the 'embeddings' extra. Install it with "
                    "`uv sync --extra embeddings`, or set TROUVEUR_EMBEDDING_PROVIDER to another "
                    "provider."
                ) from exc
            model = TextEmbedding(model_name=self.model)
            self._check(model)
            self._model = model
        return self._model

    @staticmethod
    def _check(model: Any) -> None:
        """Refuse a model build that cannot produce a usable vector.

        The ONNX build of jina-embeddings-v2-base-de returns all-NaN through this exact path.
        NaN does not raise: it flows into the column and cosine turns the index into noise that
        looks like a working search returning bad results.
        """
        import math

        probe = next(iter(model.embed(["Backend Engineer"])), None)
        if probe is None or not all(math.isfinite(float(value)) for value in probe):
            raise RuntimeError(
                "This build of the embedding model returns non-finite vectors; refusing to "
                "write them, because cosine over NaN is silent nonsense rather than an error."
            )

    document_prefix = DOCUMENT_PREFIX
    query_prefix = QUERY_PREFIX

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"{self.document_prefix}{text}" for text in texts])

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"{self.query_prefix}{text}" for text in texts])

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # onnxruntime is synchronous and CPU-bound; inline it would stall the event loop.
        return await asyncio.to_thread(self._embed_sync, texts)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = [vector.tolist() for vector in model.embed(texts, batch_size=_BATCH)]
        for vector in vectors:
            if len(vector) != self.dim:
                raise RuntimeError(
                    f"{MODEL} returned {len(vector)} dimensions but the embedding column is "
                    f"halfvec({self.dim}); changing model width is a migration."
                )
        return vectors


class LocalOnnxMpnetProvider(LocalOnnxProvider):
    """The same local path with a stronger multilingual model, at 768 dimensions.

    Roughly 2.5x the compute per document, so switching is a backfill measured in days, and it
    needs the 768-wide column.
    """

    name = "local-onnx-mpnet"
    model = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    dim = 768
