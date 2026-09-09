"""The evaluation set must stay adversarial, or it measures nothing.

A T2 or T3 needle that shares a content word with its persona's own queries is findable by
lexical retrieval, which is exactly what those tiers exist to rule out. Writing the needles by
hand, four of twelve leaked a word on the first pass -- "startup", "maintenance", "Auswertung" --
and each would have quietly turned a dense-retrieval result into a BM25 result.

The overlap check is a heuristic: Postgres stems and folds rather than splitting on words. It is
deliberately stricter than the real matcher, because a needle that is merely close to leaking is
not worth defending.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from trouveur.match.expand import deterministic_queries
from trouveur.models import UserProfile, fold

DATA = Path(__file__).resolve().parents[2] / "trouveur" / "eval" / "data"
PERSONAS = {p["key"]: p for p in json.loads((DATA / "personas.json").read_text("utf-8"))}
NEEDLES = json.loads((DATA / "needles.json").read_text("utf-8"))

# Short words carry no discriminating signal and would make the check noise.
MIN_WORD = 5


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"\w+", fold(text)) if len(word) >= MIN_WORD}


def _searchable(needle: dict) -> str:
    """Everything Postgres indexes for this posting: title plus description."""
    if needle["source"] == "greenhouse":
        body = HTMLParser(
            html.unescape(needle["listing"].get("content") or "")
        ).text(separator=" ")
        return f"{needle['listing']['title']} {body}"
    body = (needle.get("detail") or {}).get("stellenangebotsBeschreibung", "")
    return f"{needle['listing']['stellenangebotsTitel']} {body}"


def _profile(key: str) -> UserProfile:
    return UserProfile(user_id=1, **PERSONAS[key]["profile"])


@pytest.mark.parametrize(
    "needle",
    [n for n in NEEDLES if n["tier"] in ("T2", "T3")],
    ids=lambda n: f"{n['tier']}-{n['id']}",
)
def test_paraphrase_needles_share_no_vocabulary_with_their_persona(needle):
    queries = deterministic_queries(_profile(needle["persona"]))
    query_words = set().union(*(_words(query) for query in queries))
    overlap = _words(_searchable(needle)) & query_words
    assert not overlap, (
        f"{needle['id']} ({needle['tier']}) shares {sorted(overlap)} with its persona's queries, "
        "so lexical retrieval can find it and the tier no longer measures dense recall."
    )


def test_every_persona_has_each_tier():
    """A persona missing a tier reports a recall of 0/0, which reads as success."""
    for key in PERSONAS:
        tiers = {n["tier"] for n in NEEDLES if n["persona"] == key}
        assert tiers == {"T1", "T2", "T3", "N"}, f"{key} is missing tiers: {tiers}"


def test_needle_ids_are_unique():
    ids = [n["id"] for n in NEEDLES]
    assert len(ids) == len(set(ids))


def test_every_needle_names_a_known_persona_and_source():
    from trouveur.sources.registry import NORMALIZERS

    for needle in NEEDLES:
        assert needle["persona"] in PERSONAS, needle["id"]
        assert needle["source"] in NORMALIZERS, needle["id"]


def test_t1_needles_do_share_vocabulary():
    """Guards the guard: if T1 needles stopped overlapping, the tiers would be indistinguishable."""
    for needle in [n for n in NEEDLES if n["tier"] == "T1"]:
        queries = deterministic_queries(_profile(needle["persona"]))
        query_words = set().union(*(_words(query) for query in queries))
        assert _words(_searchable(needle)) & query_words, (
            f"{needle['id']} is a T1 needle but shares nothing lexical with its persona"
        )
