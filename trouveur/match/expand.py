"""Profile -> the artifacts derived once per profile version, never once per job.

The deterministic expansion is the FLOOR, not a degraded fallback: retrieval must work fully
with no API key at all.
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

# Small on purpose: this is the one piece of the profile whose length is multiplied by however many
# postings a run scores, rather than paid once.
SUMMARY_MAX_CHARS = 700
_SUMMARY_MAX_TOKENS = 300

# Eight: the evaluation found the gain flat from five and negative by fifteen, where the adverts
# repeat each other and RRF promotes what they have in common.
MAX_ADVERTS = 8
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


_SUMMARY_SYSTEM = """You compress a candidate's own account of their background into evidence a
screener can use.

Write at most 80 words, as compact clauses rather than sentences. Keep only what shows capability:
domains worked in, the systems, tools, methods and standards actually used, the scale and kind of
employer, qualifications, and languages of work. Keep the concrete nouns -- they are the whole
point -- and keep them in the language they were written in.

Drop aspiration, motivation, what the candidate is looking for, and every adjective that is not
load-bearing. Do not infer or invent anything that is not in the text.

Return ONLY the summary text, no preamble and no formatting."""


def deterministic_queries(profile: UserProfile) -> list[str]:
    """Queries derivable from the profile alone, with no model and no network.

    The background is NOT among them: `websearch_to_tsquery` ANDs its terms, so a paragraph
    becomes a hundred-term conjunction that matches nothing.
    """
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
    """What both generators see. The background goes in WHOLE: this call is billed once per
    profile version, so it is the one place the full text is affordable."""
    return (
        f"current title: {profile.title or 'unstated'}\n"
        f"years of experience: {profile.years_experience}\n"
        f"objectives: {profile.objectives or 'unstated'}\n"
        f"background: {profile.background or 'unstated'}\n"
        f"must have: {'; '.join(profile.must_have) or 'none stated'}\n"
        f"keywords the candidate already uses: {', '.join(profile.keywords) or 'none'}"
    )


def truncated_background(profile: UserProfile) -> str:
    """The floor for the reranker when no summary could be generated."""
    return profile.background.strip()[:SUMMARY_MAX_CHARS]


def _as_text(item: object) -> str:
    """One array element as one query.

    Asked for strings, the model returns objects roughly one call in three. Rejecting those cost
    the whole response and degraded expansion to deterministic, silently.
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

    A separate call, because the long output is where a cheap model's JSON falls apart and losing
    it must not also lose the phrases.
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


async def summarise_background(
    settings: Settings, profile: UserProfile, *, api_key: str, model: str, provider_pin: str | None
) -> tuple[str, llm.Usage]:
    """The background as the reranker will see it, distilled once per profile version.

    A third independently survivable call, for the reason the adverts are already separate.
    """
    completion = await llm.complete(
        api_key=api_key,
        model=model,
        provider_pin=provider_pin,
        system=_SUMMARY_SYSTEM,
        user=profile.background,
        max_tokens=_SUMMARY_MAX_TOKENS,
        timeout=settings.llm_timeout_seconds,
    )
    return completion.text.strip()[:SUMMARY_MAX_CHARS], completion.usage


def combine(deterministic: list[str], expanded: list[str]) -> list[str]:
    """The user's own words first, so the cap bites on generated vocabulary rather than theirs."""
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
    "SUMMARY_MAX_CHARS",
    "build_prompt",
    "combine",
    "deterministic_queries",
    "expand_adverts",
    "expand_with_model",
    "parse_response",
    "summarise_background",
    "truncated_background",
]
