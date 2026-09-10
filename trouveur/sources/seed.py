"""Reading an operator's local tenant list.

A tenant-scoped source is dropped from the run until somebody names its boards, and there is no
index of tenants anywhere for most of these platforms. Until the discovery pass exists, the list
has to be typed by hand -- and typing it one `trouveur tenants add` at a time does not survive a
database reset.

So this reads a **local, unversioned** file and hands it to the same writer the CLI already uses.
It is a stopgap with a deliberate shape:

  - **the file is not the crawl set; the database is.** This only ever inserts, through
    `admin.add_tenants`, which is idempotent and never re-enables a tenant an operator has
    switched off. Deleting a line here does not remove a board -- `trouveur tenants remove` does.
    Two writers owning one table is exactly the rot the crawl set was split in two to avoid.
  - **it stays out of git.** The corpus a commit produces is already not reproducible from that
    commit alone (see AGENTS.md), and committing one installation's board list would imply
    otherwise while quietly making everyone crawl the same companies.
  - **every entry is validated at the write**, by the source's own grammar, so a typo is a startup
    error rather than a 404 on every sweep for ever.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from trouveur.sources.errors import SourceError
from trouveur.sources.registry import SOURCES, clean_scope

# Looked for in the working directory when no path is given. The name says both that it is local
# and that it is not the authority.
DEFAULT_FILENAME = "tenants.local.toml"


def load_seed(path: Path) -> dict[str, list[str]]:
    """Parse a seed file into {source: [scope]}, or explain exactly what is wrong with it.

    Every value is cleaned by the owning source's grammar, so a full careers URL is accepted
    wherever a slug is -- pasting the URL is the obvious mistake and the slug is recoverable
    from it.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SourceError(
            f"No tenant seed file at {path}. Copy {DEFAULT_FILENAME}.example to {path} and list "
            "the boards to crawl, or pass a path."
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise SourceError(f"{path} is not valid TOML: {exc}") from exc

    seed: dict[str, list[str]] = {}
    for source, values in raw.items():
        if source not in SOURCES:
            known = ", ".join(sorted(name for name, s in SOURCES.items() if s.tenant_scoped))
            raise SourceError(
                f"{path}: unknown source {source!r}. Sources that take tenants are: {known}."
            )
        if not isinstance(values, list):
            raise SourceError(
                f"{path}: {source} must be a list of boards, not {type(values).__name__}. "
                f'Write it as {source} = ["one", "two"].'
            )
        cleaned = [clean_scope(source, str(value)) for value in values]
        # Re-listing a board is a harmless duplicate in a hand-edited file; order is kept so the
        # log reads in the order the operator wrote them.
        seed[source] = list(dict.fromkeys(cleaned))
    return seed
