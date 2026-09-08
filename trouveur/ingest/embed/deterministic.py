"""A reproducible stand-in provider, for tests and for exercising the pipeline without a model.

Its vectors carry no semantics whatsoever. It exists so the ingest and retrieval paths can be
tested end to end with no model download and no network, which is the only way those tests stay
fast enough to run on every change.

It is safe to leave selectable in production precisely because it is not silent: the rows it
writes carry embedding_version "deterministic:sha256:384", so a database running on it is obvious
from the data rather than only from a config file nobody reads.
"""

from __future__ import annotations

import hashlib
import math
import struct

from trouveur.ingest.embed.base import EMBEDDING_DIM


class DeterministicProvider:
    name = "deterministic"
    model = "sha256"
    dim = EMBEDDING_DIM

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(f"passage: {text}") for text in texts]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(f"query: {text}") for text in texts]

    def _vector(self, text: str) -> list[float]:
        raw = b""
        counter = 0
        while len(raw) < self.dim * 4:
            raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
            counter += 1
        values = list(struct.unpack(f"{self.dim}f", raw[: self.dim * 4]))
        values = [0.0 if math.isnan(v) or math.isinf(v) else v for v in values]
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]
