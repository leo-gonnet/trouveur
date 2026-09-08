"""Local ONNX embeddings. No network, no per-document cost, no torch.

multilingual-e5-small handles German and English in one vector space, which this corpus needs: an
Austrian advert and its English equivalent must land near each other.

The e5 family is trained with "passage: " and "query: " prefixes and loses noticeable recall
without them. They are applied here rather than by callers, because a caller that forgets is
indistinguishable from one that remembers until you measure retrieval quality.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trouveur.ingest.embed.base import EMBEDDING_DIM

log = logging.getLogger(__name__)

MODEL = "intfloat/multilingual-e5-small"
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

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"passage: {text}" for text in texts])

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"query: {text}" for text in texts])

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
