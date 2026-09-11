"""The one place source-specific knowledge is dispatched.

Everything downstream addresses a source by name and never branches on which source it is. Adding
a source is an entry in the table below plus its own package -- if a change requires editing the
ingest pipeline, the matcher or the web layer as well, the abstraction has leaked and the fix
belongs here rather than there.

The table is declarative on purpose. Twelve hand-written constructor calls is the branching this
module exists to prevent, and every one of them would need remembering when a new capability --
a scope grammar, a delta window -- is added to the family.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trouveur.models import CanonicalJob
from trouveur.sources import (
    arbeitnow,
    arbeitsagentur,
    ashby,
    breezy,
    greenhouse,
    himalayas,
    jobicy,
    lever,
    personio,
    rippling,
    workable,
    workday,
)
from trouveur.sources.base import Source
from trouveur.sources.errors import SourceError
from trouveur.sources.scopes import dotted_slug_scope, slug_scope, workday_scope

NormalizeFn = Callable[..., CanonicalJob | None]
ScopeCleaner = Callable[[str], str]


@dataclass(frozen=True)
class SourceSpec:
    """Everything the rest of the system needs to know about one source.

    `normalize_version` is stored on every row the normaliser writes, so raising it is what makes
    the whole corpus eligible for re-derivation.
    """

    normalize: NormalizeFn
    normalize_version: int
    # Built with the source's tenant list when it is tenant-scoped, and with nothing when it is
    # not. Kept as a factory so assembling a run stays free of I/O and of per-source branching.
    build: Callable[[list[str]], Source]
    tenant_scoped: bool
    # How an operator's paste becomes a tenant identifier. Only meaningful when tenant_scoped.
    clean_scope: ScopeCleaner = slug_scope


SOURCES: dict[str, SourceSpec] = {
    arbeitsagentur.SOURCE: SourceSpec(
        normalize=arbeitsagentur.normalize,
        normalize_version=arbeitsagentur.version,
        build=lambda _scopes: arbeitsagentur.ArbeitsagenturSource(),
        tenant_scoped=False,
    ),
    greenhouse.SOURCE: SourceSpec(
        normalize=greenhouse.normalize,
        normalize_version=greenhouse.version,
        build=lambda scopes: greenhouse.GreenhouseSource(boards=scopes),
        tenant_scoped=True,
    ),
    ashby.SOURCE: SourceSpec(
        normalize=ashby.normalize,
        normalize_version=ashby.version,
        build=lambda scopes: ashby.AshbySource(boards=scopes),
        tenant_scoped=True,
        # Ashby boards may be registered under a domain, e.g. 'mistral.ai'.
        clean_scope=dotted_slug_scope,
    ),
    lever.SOURCE: SourceSpec(
        normalize=lever.normalize,
        normalize_version=lever.version,
        build=lambda scopes: lever.LeverSource(boards=scopes),
        tenant_scoped=True,
    ),
    breezy.SOURCE: SourceSpec(
        normalize=breezy.normalize,
        normalize_version=breezy.version,
        build=lambda scopes: breezy.BreezySource(boards=scopes),
        tenant_scoped=True,
    ),
    rippling.SOURCE: SourceSpec(
        normalize=rippling.normalize,
        normalize_version=rippling.version,
        build=lambda scopes: rippling.RipplingSource(boards=scopes),
        tenant_scoped=True,
    ),
    personio.SOURCE: SourceSpec(
        normalize=personio.normalize,
        normalize_version=personio.version,
        build=lambda scopes: personio.PersonioSource(boards=scopes),
        tenant_scoped=True,
    ),
    workday.SOURCE: SourceSpec(
        normalize=workday.normalize,
        normalize_version=workday.version,
        build=lambda scopes: workday.WorkdaySource(boards=scopes),
        tenant_scoped=True,
        # A Workday board is three facts, not a slug; the default grammar rejects it outright.
        clean_scope=workday_scope,
    ),
    workable.SOURCE: SourceSpec(
        normalize=workable.normalize,
        normalize_version=workable.version,
        build=lambda _scopes: workable.WorkableSource(),
        tenant_scoped=False,
    ),
    arbeitnow.SOURCE: SourceSpec(
        normalize=arbeitnow.normalize,
        normalize_version=arbeitnow.version,
        build=lambda _scopes: arbeitnow.ArbeitnowSource(),
        tenant_scoped=False,
    ),
    himalayas.SOURCE: SourceSpec(
        normalize=himalayas.normalize,
        normalize_version=himalayas.version,
        build=lambda _scopes: himalayas.HimalayasSource(),
        tenant_scoped=False,
    ),
    jobicy.SOURCE: SourceSpec(
        normalize=jobicy.normalize,
        normalize_version=jobicy.version,
        build=lambda _scopes: jobicy.JobicySource(),
        tenant_scoped=False,
    ),
}

# Kept as a mapping of its own because persist(), the golden tests and the architecture guard all
# ask the same question -- "which sources normalise, and at what version" -- and none of them
# should have to know about the rest of a spec.
NORMALIZERS: dict[str, tuple[NormalizeFn, int]] = {
    name: (spec.normalize, spec.normalize_version) for name, spec in SOURCES.items()
}


def normalizer_for(source: str) -> tuple[NormalizeFn, int]:
    try:
        return NORMALIZERS[source]
    except KeyError:
        raise SourceError(
            f"No normaliser is registered for source {source!r}; add it to "
            "trouveur.sources.registry.SOURCES."
        ) from None


def clean_scope(source: str, raw: str) -> str:
    """Turn one operator-supplied entry into a tenant identifier, by the source's own grammar.

    Validation happens here, at the write, because a malformed scope that reaches `source_tenant`
    fails on every sweep afterwards and surfaces only as a slowly growing failure count.
    """
    try:
        spec = SOURCES[source]
    except KeyError:
        known = ", ".join(sorted(SOURCES))
        raise SourceError(f"Unknown source {source!r}; known sources are: {known}.") from None
    if not spec.tenant_scoped:
        raise SourceError(
            f"{source!r} sweeps one global corpus and has no tenants, so {raw!r} cannot be "
            "registered for it."
        )
    return spec.clean_scope(raw)


def build_sources(
    *, tenants: dict[str, list[str]] | None = None, only: str | None = None
) -> list[Source]:
    """Assemble the sources for a run.

    Tenants arrive as a mapping keyed by source name, so the caller loads the whole crawl set with
    one query and never branches on which sources happen to be tenant-scoped. Sources still do no
    I/O of their own here, so a sweep can be exercised against a stub transport with no database.

    A tenant-scoped source with no tenants is dropped rather than run: sweeping it would make no
    requests, find nothing, and report a perfectly healthy empty sweep.
    """
    tenants = tenants or {}
    if only is not None and only not in SOURCES:
        known = ", ".join(sorted(SOURCES))
        raise SourceError(f"Unknown source {only!r}; known sources are: {known}.")

    sources: list[Source] = []
    for name, spec in SOURCES.items():
        if only is not None and name != only:
            continue
        scopes = tenants.get(name, [])
        if spec.tenant_scoped and not scopes:
            continue
        sources.append(spec.build(scopes))
    return sources
