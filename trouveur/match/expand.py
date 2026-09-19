"""Profile -> retrieval queries.

Expansion happens once per profile version, not once per job. That is what makes it affordable to
use a model here at all: one call when a user edits their profile, reused by every retrieval that
profile performs afterwards.

The deterministic expansion is not a fallback that degrades quietly -- it is the floor. Retrieval
works with no API key at all; a model only ever adds vocabulary the user did not think to type,
which is precisely the recall gap keyword search leaves.
"""

from __future__ import annotations

import json
import logging

from trouveur.config import Settings
from trouveur.match import llm
from trouveur.models import UserProfile
from trouveur.versions import QUERY_EXPANSION_VERSION

log = logging.getLogger(__name__)

MAX_QUERIES = 8
_MAX_TOKENS = 400

# Synthetic adverts for the dense arm. Eight, because the evaluation found the gain flat from
# five to eight and negative by fifteen: past that the adverts repeat each other, and RRF over
# many near-identical lists promotes whatever they have in common rather than what each found.
MAX_ADVERTS = 8
# Enough for eight short adverts with headroom. Asking one cheap model for fifteen long ones in
# a single call failed outright in two personas of three -- unparseable JSON, a single 2,593-word
# blob, or the ceiling hit mid-array.
_ADVERT_MAX_TOKENS = 2000

_SYSTEM = """You turn a candidate's profile into search queries for a job database.

Return 5 to 8 short queries, each a noun phrase a job advert would actually contain. Cover the
role's common synonyms and adjacent titles, in German and in English, because the corpus is
DACH-wide and mixes both languages. Include the specialisations the candidate's objectives imply.

Do not include locations, seniority words, salary, or company names: those are filtered
structurally and would only narrow the search twice.

Return ONLY a JSON array of strings."""


_ADVERT_SYSTEM = """You write synthetic job adverts that will be used as search queries.

Given a candidate profile, write 8 job adverts for 8 DIFFERENT roles this candidate would be
hired into NEXT. Rules:

- Write the role they are moving TO, never the role they already have. If the objectives
  describe a transition, every advert is on the far side of it.
- No two adverts may share a job title. Span the plausible range of destinations, including the
  ones the candidate would not have thought to search for themselves.
- Start each advert with the job title, then the responsibilities, then the requirements. Only
  the first 120 tokens are ever read, so put the discriminating words first and do not write a
  company boilerplate paragraph.
- About 70 words each. Use the vocabulary a real advert for that role uses -- the tools,
  methods and standards named explicitly.
- Match the candidate's languages; the corpus is DACH-wide and mixes German and English.

No company names, no locations, no salary, no seniority labels.

Return ONLY a JSON array of 8 strings."""


def deterministic_queries(profile: UserProfile) -> list[str]:
    """Queries derivable from the profile alone, with no model and no network."""
    queries: list[str] = []
    if profile.title:
        queries.append(profile.title)
    queries.extend(keyword for keyword in profile.keywords if keyword.strip())
    queries.extend(item for item in profile.must_have if item.strip())
    if profile.objectives:
        queries.append(profile.objectives[:200])

    seen: dict[str, None] = {}
    for query in queries:
        cleaned = query.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)[:MAX_QUERIES]


def build_prompt(profile: UserProfile) -> str:
    return (
        f"current title: {profile.title or 'unstated'}\n"
        f"years of experience: {profile.years_experience}\n"
        f"objectives: {profile.objectives or 'unstated'}\n"
        f"must have: {'; '.join(profile.must_have) or 'none stated'}\n"
        f"keywords the candidate already uses: {', '.join(profile.keywords) or 'none'}"
    )


def _as_text(item: object) -> str:
    """One array element as one query.

    Asked for an array of strings, the model returns an array of {"title": ..., "description":
    ...} objects a good fraction of the time -- measured at roughly one call in three on the
    pinned model. Rejecting those cost the whole response: every element failed the string check,
    parse_response returned nothing, and expansion degraded to deterministic silently. Joining an
    object's string values keeps the content and costs nothing when the model complies.
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        parts = [value.strip() for value in item.values() if isinstance(value, str)]
        return ". ".join(part for part in parts if part)
    return ""


def parse_response(text: str, *, limit: int = MAX_QUERIES) -> list[str]:
    """A malformed response yields nothing, so the caller falls back rather than searching junk."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        log.warning("query expansion response contained no JSON array")
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        log.warning("query expansion response was not valid JSON")
        return []
    if not isinstance(payload, list):
        return []
    return [query for query in map(_as_text, payload) if query][:limit]


async def expand_with_model(
    settings: Settings, profile: UserProfile, *, api_key: str, model: str, provider_pin: str | None
) -> tuple[list[str], llm.Usage]:
    completion = await llm.complete(
        api_key=api_key,
        model=model,
        provider_pin=provider_pin,
        system=_SYSTEM,
        user=build_prompt(profile),
        max_tokens=_MAX_TOKENS,
        timeout=settings.llm_timeout_seconds,
    )
    return parse_response(completion.text), completion.usage


async def expand_adverts(
    settings: Settings, profile: UserProfile, *, api_key: str, model: str, provider_pin: str | None
) -> tuple[list[str], llm.Usage]:
    """Synthetic adverts for the dense arm: a vector where the target adverts live.

    A second call rather than a second field in the first one. The two artifacts have very
    different output lengths, and the long one is where a cheap model's JSON falls apart -- so a
    failure here leaves the phrases intact instead of losing both.

    Sent only to the dense retriever (see retrieve.retrieve_arms), and added to the user's own
    queries rather than replacing them: substituting cost the needles that share the profile's
    vocabulary, one falling from rank 31 to 341.
    """
    completion = await llm.complete(
        api_key=api_key,
        model=model,
        provider_pin=provider_pin,
        system=_ADVERT_SYSTEM,
        user=build_prompt(profile),
        max_tokens=_ADVERT_MAX_TOKENS,
        timeout=settings.llm_timeout_seconds,
    )
    return parse_response(completion.text, limit=MAX_ADVERTS), completion.usage


def combine(deterministic: list[str], expanded: list[str]) -> list[str]:
    """The user's own words first, then the model's additions.

    Order matters: the deterministic queries are what the user actually asked for, and fusion
    weights earlier lists no differently, but keeping them first makes the cap bite on generated
    vocabulary rather than on the user's own.
    """
    combined: dict[str, str] = {}
    for query in [*deterministic, *expanded]:
        cleaned = query.strip()
        key = cleaned.lower()
        if cleaned and key not in combined:
            combined[key] = cleaned
    return list(combined.values())[:MAX_QUERIES]


__all__ = [
    "MAX_ADVERTS",
    "MAX_QUERIES",
    "QUERY_EXPANSION_VERSION",
    "build_prompt",
    "combine",
    "deterministic_queries",
    "expand_adverts",
    "expand_with_model",
    "parse_response",
]
