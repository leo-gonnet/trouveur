"""NUTS codes and their names, as EURES labels them. Read from `nuts.tsv`, built by
tools/build_nuts.py. Pure: the file is read once and never written."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

# EURES writes Greece as EL, its NUTS code, where every reader's country is ISO 3166.
_ISO_TO_NUTS = {"GR": "EL"}


@lru_cache(maxsize=1)
def _labels() -> dict[str, str]:
    text = files("trouveur.sources.eures").joinpath("nuts.tsv").read_text(encoding="utf-8")
    return dict(line.split("\t", 1) for line in text.splitlines() if "\t" in line)


def label(code: str) -> str | None:
    return _labels().get(code.strip().upper())


def country_code(iso: str) -> str | None:
    """The NUTS country code EURES filters by, or None for a country EURES does not cover."""
    code = _ISO_TO_NUTS.get(iso.upper(), iso.upper())
    return code if len(code) == 2 and code in _labels() else None
