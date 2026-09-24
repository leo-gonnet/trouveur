"""Tenant-identifier grammars. Each source names the one it uses; dispatch stays in the registry."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from trouveur.sources.errors import SourceError

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Ashby only: `roadsurfer.com` and `mistral.ai` are live boards. Not global -- for every other
# source a dotted slug is a pasted homepage.
_DOTTED_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)*$")

# Case is preserved: the API 404s on a lowercased site name.
_WORKDAY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*:wd[0-9]+:[A-Za-z0-9][A-Za-z0-9_-]*$")
_WORKDAY_HOST = re.compile(r"^([A-Za-z0-9-]+)\.(wd[0-9]+)\.myworkdayjobs\.com$")


def is_valid_scope(scope: str) -> bool:
    return bool(_SLUG.match(scope))


def slug_scope(raw: str) -> str:
    """The default: one lowercase path segment, or a careers URL to take it from."""
    candidate = _last_segment(raw)
    if not is_valid_scope(candidate):
        raise SourceError(
            f"{raw!r} is not a valid tenant slug: expected a URL path segment such as 'gitlab'."
        )
    return candidate


def dotted_slug_scope(raw: str) -> str:
    """A path segment that may itself be a domain."""
    candidate = _last_segment(raw)
    if not _DOTTED_SLUG.match(candidate):
        raise SourceError(
            f"{raw!r} is not a valid tenant slug: expected a URL path segment such as 'ramp' or "
            "'mistral.ai'."
        )
    return candidate


def _last_segment(raw: str) -> str:
    """A bare slug, or the last path segment of a pasted careers URL."""
    candidate = raw.strip().rstrip("/").lower()
    if "/" in candidate:
        candidate = candidate.rsplit("/", 1)[-1]
    return candidate


def workday_scope(raw: str) -> str:
    """`tenant:instance:site`, from either that form or a pasted careers URL."""
    candidate = raw.strip().rstrip("/")
    if _WORKDAY.match(candidate):
        return candidate

    parts = urlsplit(candidate if "//" in candidate else f"https://{candidate}")
    host = _WORKDAY_HOST.match(parts.netloc)
    segments = [segment for segment in parts.path.split("/") if segment]
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
