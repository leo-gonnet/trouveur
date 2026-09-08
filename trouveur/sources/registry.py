"""The one place source-specific knowledge is dispatched.

Everything downstream addresses a source by name and never branches on which source it is. Adding
a source is an entry in these two tables plus its own package -- if a change requires editing the
ingest pipeline, the matcher or the web layer as well, the abstraction has leaked and the fix
belongs here rather than there.
"""

from __future__ import annotations

from collections.abc import Callable

from trouveur.models import CanonicalJob
from trouveur.sources import arbeitsagentur, greenhouse
from trouveur.sources.base import Source
from trouveur.sources.errors import SourceError

NormalizeFn = Callable[..., CanonicalJob | None]

# source name -> (pure normaliser, its version). The version is stored on every row the
# normaliser writes, so raising it here is what makes the whole corpus eligible for re-derivation.
NORMALIZERS: dict[str, tuple[NormalizeFn, int]] = {
    arbeitsagentur.SOURCE: (arbeitsagentur.normalize, arbeitsagentur.version),
    greenhouse.SOURCE: (greenhouse.normalize, greenhouse.version),
}


def normalizer_for(source: str) -> tuple[NormalizeFn, int]:
    try:
        return NORMALIZERS[source]
    except KeyError:
        raise SourceError(
            f"No normaliser is registered for source {source!r}; add it to "
            "trouveur.sources.registry.NORMALIZERS."
        ) from None


def build_sources(*, only: str | None = None) -> list[Source]:
    """Assemble the sources for a run.

    Sources read their own configuration from the repository and never touch the database, so a
    sweep can be exercised against a stub transport with no Postgres anywhere in the test, and the
    corpus a given commit produces is reproducible from that commit.
    """
    sources: list[Source] = [
        arbeitsagentur.ArbeitsagenturSource(),
        greenhouse.GreenhouseSource(),
    ]

    if only:
        sources = [source for source in sources if source.name == only]
        if not sources:
            known = ", ".join(sorted(NORMALIZERS))
            raise SourceError(f"Unknown source {only!r}; known sources are: {known}.")
    return sources
