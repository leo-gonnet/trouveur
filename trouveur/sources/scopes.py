"""Loading a source's tenant registry from the repository.

Some sources publish no index of their tenants, so the only list that exists is the one we keep.
That list is **configuration**, not data: a human chooses which companies this installation
crawls, the choice belongs in a reviewable diff, and the corpus a given commit produces should be
reproducible from that commit alone.

It deliberately does not live in the database and is deliberately not editable in the UI. The
crawl set is shared by every user, so it is an operator decision rather than a per-user setting —
one user adding five hundred boards would make everyone pay for the crawl. What the database keeps
is the *observation*: which scopes answered, which failed, and for how long. See
`source_scope_health`.
"""

from __future__ import annotations

import re
from pathlib import Path

from trouveur.sources.errors import SourceError

# Deliberately strict. A slug is a URL path segment; anything else is a typo that would otherwise
# become a 404 every single day, quietly, forever.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def load_scopes(path: Path) -> list[str]:
    """Read one scope per line. `#` starts a comment; blank lines are ignored.

    Order is not preserved as significant, but duplicates are dropped so that a slug appearing
    twice does not double the requests made to that tenant.
    """
    if not path.exists():
        raise SourceError(f"The scope registry {path} does not exist.")

    scopes: dict[str, None] = {}
    problems: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not _SLUG.match(line):
            problems.append(f"line {number}: {line!r}")
            continue
        scopes.setdefault(line, None)

    if problems:
        raise SourceError(
            f"{path.name} contains entries that are not valid slugs: {'; '.join(problems)}."
        )
    return list(scopes)
