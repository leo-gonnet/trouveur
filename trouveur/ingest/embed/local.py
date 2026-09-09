"""Local ONNX embeddings. No network, no per-document cost, no torch.

The model handles German and English in one vector space, which this corpus needs: an Austrian
advert and its English equivalent must land near each other.

Prefixes are a property of the model, declared here rather than applied by callers. This one is
symmetric -- trained for sentence similarity, so a query and a document are encoded identically
and it takes no prefix. The e5 family is the opposite: it expects "passage: " and "query: " and
loses noticeable recall without them. Getting this backwards is silent either way, so a model
swap must set the prefixes alongside the name, and both belong in one place.
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
            self._model = TextEmbedding(model_name=MODEL)
        return self._model

    document_prefix = DOCUMENT_PREFIX
    query_prefix = QUERY_PREFIX

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"{self.document_prefix}{text}" for text in texts])

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"{self.query_prefix}{text}" for text in texts])

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # onnxruntime is synchronous and CPU-bound; running it inline would stall the event loop
        # for the whole batch and starve every concurrent fetch.
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
