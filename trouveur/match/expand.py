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

_SYSTEM = """You turn a candidate's profile into search queries for a job database.

Return 5 to 8 short queries, each a noun phrase a job advert would actually contain. Cover the
role's common synonyms and adjacent titles, in German and in English, because the corpus is
DACH-wide and mixes both languages. Include the specialisations the candidate's objectives imply.

Do not include locations, seniority words, salary, or company names: those are filtered
structurally and would only narrow the search twice.

Return ONLY a JSON array of strings."""


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


def parse_response(text: str) -> list[str]:
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
    return [item.strip() for item in payload if isinstance(item, str) and item.strip()][
        :MAX_QUERIES
    ]


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
    "MAX_QUERIES",
    "QUERY_EXPANSION_VERSION",
    "build_prompt",
    "combine",
    "deterministic_queries",
    "expand_with_model",
    "parse_response",
]
