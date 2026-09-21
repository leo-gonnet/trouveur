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
            model = TextEmbedding(model_name=self.model)
            # Probed once, at load, not per batch: the cost is one embedding for the life of the
            # process.
            self._check(model)
            self._model = model
        return self._model

    @staticmethod
    def _check(model: Any) -> None:
        """Refuse a model build that cannot produce a usable vector.

        Not paranoia: the ONNX build of jina-embeddings-v2-base-de returns all-NaN vectors
        through this exact path. NaN does not raise -- it flows into the column, and cosine
        turns the whole ANN index into noise that looks like a working search returning bad
        results. One probe at load is cheaper than discovering that from a user's digest.
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


class LocalOnnxMpnetProvider(LocalOnnxProvider):
    """The same local path with a stronger multilingual model, at 768 dimensions.

    Measured against the incumbent over an identical pool of 2,530 postings, the planted needles
    whose adverts use different words for the same role went from 1 of 6 inside the top 50 to 4
    of 6, and the adjacent-role ones from 1 of 6 to 5 of 6 -- a larger gain than any query-side
    change produced, with the plain deterministic queries and no adverts at all.

    It costs roughly two and a half times the compute per document, so switching is a backfill
    measured in days on a four-core host, and it needs the 768-wide column: get_provider refuses
    a width the column cannot hold rather than truncating to fit.
    """

    name = "local-onnx-mpnet"
    model = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    dim = 768
