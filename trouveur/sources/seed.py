"""Reading an operator's local tenant list.

A preload, not the crawl set: it only ever inserts, so deleting a line removes nothing and a
board an operator disabled stays disabled. Gitignored.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from trouveur.sources.errors import SourceError
from trouveur.sources.registry import SOURCES, clean_scope

DEFAULT_FILENAME = "tenants.local.toml"


def load_seed(path: Path) -> dict[str, list[str]]:
    """Parse a seed file into {source: [scope]}, or explain exactly what is wrong with it."""
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
        seed[source] = list(dict.fromkeys(cleaned))
    return seed
