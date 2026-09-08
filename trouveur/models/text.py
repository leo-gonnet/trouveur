"""Text folding, used by hashing, dedupe markers and the trigram search column.

One implementation, deliberately. Postgres does the same folding with unaccent() for the trigram
index; if these two ever disagree, substring search silently stops matching the rows it indexed.
"""

from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def fold(text: str) -> str:
    """Lowercase and strip diacritics: 'München' -> 'munchen'."""
    lowered = text.lower().replace("ß", "ss")
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_for_hash(text: str | None) -> str:
    if not text:
        return ""
    return _SPACE.sub(" ", _PUNCT.sub(" ", fold(text))).strip()


def collapse_whitespace(text: str | None) -> str | None:
    if not text:
        return None
    return _SPACE.sub(" ", text).strip() or None
