"""Validating a tenant identifier.

The registry itself lives in the database (`source_tenant`), because it is written by more than
one thing: an operator through the CLI today, a discovery pass later. What stays here is the one
rule about what a scope may look like, so the CLI and any future discovery writer cannot disagree
about it.

Validation happens at the write, not at the read. A malformed slug that reaches the table would
otherwise 404 on every sweep, quietly, until somebody read the health panel closely.
"""

from __future__ import annotations

import re

from trouveur.sources.errors import SourceError

# A scope is a URL path segment. Anything else is a typo or a pasted full URL.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def is_valid_scope(scope: str) -> bool:
    return bool(_SLUG.match(scope))


def clean_scope(raw: str) -> str:
    """Normalise one operator-supplied entry, or explain why it cannot be one.

    Accepts a bare slug or a full careers URL, since pasting the URL is the obvious mistake and
    the slug is unambiguously its last path segment.
    """
    candidate = raw.strip().rstrip("/").lower()
    if "/" in candidate:
        candidate = candidate.rsplit("/", 1)[-1]
    if not is_valid_scope(candidate):
        raise SourceError(
            f"{raw!r} is not a valid tenant slug: expected a URL path segment such as 'gitlab'."
        )
    return candidate
