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


def build_sources(
    *, tenants: dict[str, list[str]] | None = None, only: str | None = None
) -> list[Source]:
    """Assemble the sources for a run.

    Tenants arrive as a mapping keyed by source name, so the caller loads the whole crawl set with
    one query and never branches on which sources happen to be tenant-scoped. Sources still do no
    I/O of their own here, so a sweep can be exercised against a stub transport with no database.

    A tenant-scoped source with no tenants is dropped rather than run: sweeping it would make no
    requests, find nothing, and report a healthy empty sweep.
    """
    tenants = tenants or {}
    sources: list[Source] = [
        arbeitsagentur.ArbeitsagenturSource(),
        greenhouse.GreenhouseSource(boards=tenants.get(greenhouse.SOURCE, [])),
    ]
    sources = [
        source
        for source in sources
        if not getattr(source, "tenant_scoped", False) or tenants.get(source.name)
    ]

    if only:
        sources = [source for source in sources if source.name == only]
        if not sources:
            known = ", ".join(sorted(NORMALIZERS))
            raise SourceError(f"Unknown source {only!r}; known sources are: {known}.")
    return sources
