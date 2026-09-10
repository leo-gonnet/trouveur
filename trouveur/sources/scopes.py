"""What a tenant identifier may look like, and how an operator's paste becomes one.

The registry itself lives in the database (`source_tenant`), because it is written by more than
one thing: an operator through the CLI today, a discovery pass later. What stays here is the
grammar -- so the CLI and any future discovery writer cannot disagree about it.

Validation happens at the write, not at the read. A malformed slug that reaches the table would
otherwise 404 on every sweep, quietly, until somebody read the health panel closely.

One grammar is not enough for every source. A Greenhouse board is a single path segment; a
Workday board is three facts (tenant, instance, site) and uppercase is ordinary in two of them.
So the rules live here and each source names the one it uses -- dispatch stays in the registry,
which is the only place that is allowed to know which source is which.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from trouveur.sources.errors import SourceError

# A scope is a URL path segment. Anything else is a typo or a pasted full URL.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Ashby lets a company register its own domain as the board name, so a dot is part of the slug
# rather than a sign someone pasted a homepage: `roadsurfer.com` and `mistral.ai` are live boards
# that the default rule silently refuses to crawl. The permission is deliberately not global --
# for every other source a dotted "slug" IS a pasted homepage, and rejecting it is the point.
_DOTTED_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)*$")

# Workday identifies a board by tenant, numbered instance and site name, all three of which are
# in the careers URL and none of which is optional. Case is preserved: the site name is
# camel-cased in the URL and the API 404s on a lowercased one.
_WORKDAY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*:wd[0-9]+:[A-Za-z0-9][A-Za-z0-9_-]*$")
_WORKDAY_HOST = re.compile(r"^([A-Za-z0-9-]+)\.(wd[0-9]+)\.myworkdayjobs\.com$")


def is_valid_scope(scope: str) -> bool:
    return bool(_SLUG.match(scope))


def slug_scope(raw: str) -> str:
    """The default: one lowercase path segment.

    Accepts a bare slug or a full careers URL, since pasting the URL is the obvious mistake and
    the slug is unambiguously its last path segment.
    """
    candidate = _last_segment(raw)
    if not is_valid_scope(candidate):
        raise SourceError(
            f"{raw!r} is not a valid tenant slug: expected a URL path segment such as 'gitlab'."
        )
    return candidate


def dotted_slug_scope(raw: str) -> str:
    """A path segment that may itself be a domain.

    Only for sources that genuinely register boards under a domain name. Everywhere else this
    would accept a pasted company homepage as a board and 404 on it every day afterwards.
    """
    candidate = _last_segment(raw)
    if not _DOTTED_SLUG.match(candidate):
        raise SourceError(
            f"{raw!r} is not a valid tenant slug: expected a URL path segment such as 'ramp' or "
            "'mistral.ai'."
        )
    return candidate


def _last_segment(raw: str) -> str:
    """A bare slug, or the last path segment of a pasted careers URL.

    Pasting the URL is the obvious mistake and the slug is unambiguously its last segment.
    """
    candidate = raw.strip().rstrip("/").lower()
    if "/" in candidate:
        candidate = candidate.rsplit("/", 1)[-1]
    return candidate


def workday_scope(raw: str) -> str:
    """`tenant:instance:site`, from either that form or a pasted careers URL.

    Verified live 2026-09-09: the three parts address different things and none can be inferred
    from the others -- the same tenant name appears on different numbered instances, and the site
    is a per-employer label. A scope missing one of them cannot be turned into a request.
    """
    candidate = raw.strip().rstrip("/")
    if _WORKDAY.match(candidate):
        return candidate

    parts = urlsplit(candidate if "//" in candidate else f"https://{candidate}")
    host = _WORKDAY_HOST.match(parts.netloc)
    segments = [segment for segment in parts.path.split("/") if segment]
    # A careers URL may carry a language prefix ("/en-US/Site"); the site is the last segment.
    if host and segments:
        return f"{host.group(1)}:{host.group(2)}:{segments[-1]}"
    raise SourceError(
        f"{raw!r} is not a valid Workday board: expected 'tenant:wdN:SiteName' or a careers URL "
        "such as 'https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite'."
    )


def split_workday_scope(scope: str) -> tuple[str, str, str]:
    """The three parts of a validated Workday scope, for the client that has to build a URL."""
    tenant, instance, site = scope.split(":", 2)
    return tenant, instance, site
