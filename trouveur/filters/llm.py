"""Stage 2: LLM relevance scoring.

The only part of this system that costs money per run, so: the rules stage runs first to keep the
input small, and every result is cached by content_hash (see AGENTS.md > LLM cost discipline).

All prompt text lives in this module. Do not scatter prompt fragments elsewhere.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from trouveur.config import Settings
from trouveur.models import ProfileData

log = logging.getLogger(__name__)

BATCH_SIZE = 10
_DESCRIPTION_CHARS = 1500
_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
# Measured ceiling was 643 output tokens for a 10-job batch. Output is billed as generated, not
# as budgeted, so this headroom is free and buys immunity to truncation losing a whole batch.
_MAX_TOKENS = 3000

_SYSTEM = """You screen job adverts for a single candidate. You are strict and concise.

Score each job 0-100 for how well it matches the candidate's profile and objectives:
  90-100  excellent fit, the candidate should apply today
  70-89   good fit, clearly worth reading
  40-69   plausible but compromised on an important dimension
  0-39    poor fit

Judge the substance of the role, not the polish of the advert. German and English adverts are
equally valid. Penalise heavily: staffing agencies, disguised sales roles, roles far junior or
far senior to the candidate, and adverts that violate a stated deal-breaker.

Return ONLY a JSON array, one object per job, no prose:
[{"id": <int>, "score": <int 0-100>, "reason": "<one sentence, max 25 words>",
  "red_flags": ["<short phrase>", ...]}]"""


class Score(BaseModel):
    id: int
    score: int = Field(ge=0, le=100)
    reason: str = ""
    red_flags: list[str] = Field(default_factory=list)


def build_prompt(profile: ProfileData, jobs: list[Any]) -> str:
    listing = []
    for row in jobs:
        where = ", ".join(filter(None, [row.location_city, row.location_country])) or "unknown"
        remote = "yes" if row.remote else ("no" if row.remote is False else "unstated")
        salary = (
            f"{row.salary_min or '?'}-{row.salary_max or '?'}"
            if (row.salary_min or row.salary_max)
            else "unstated"
        )
        description = (row.description or "")[:_DESCRIPTION_CHARS]
        listing.append(
            f"### id={row.id}\n"
            f"title: {row.title}\n"
            f"company: {row.company or 'unknown'}\n"
            f"location: {where} | remote: {remote} | salary: {salary}\n"
            f"description: {description}"
        )

    return (
        f"CANDIDATE PROFILE\n"
        f"current title: {profile.title}\n"
        f"years of experience: {profile.years_experience}\n"
        f"languages: {', '.join(profile.languages) or 'unstated'}\n"
        f"objectives: {profile.objectives}\n"
        f"must have: {'; '.join(profile.must_have) or 'none stated'}\n"
        f"deal breakers: {'; '.join(profile.deal_breakers) or 'none stated'}\n"
        f"minimum salary: {int(profile.min_salary_eur_year)} EUR/year\n\n"
        f"JOBS TO SCORE ({len(jobs)})\n\n" + "\n\n".join(listing)
    )


def parse_response(text: str) -> list[Score]:
    """Parse the model's JSON array. A malformed response yields nothing, never a zero score."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned.lstrip("`")
        cleaned = cleaned.removeprefix("json").strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        log.warning("LLM response contained no JSON array; treating batch as unscored")
        return []
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        log.warning("LLM response was not valid JSON; treating batch as unscored")
        return []

    scores = []
    for entry in payload if isinstance(payload, list) else []:
        try:
            scores.append(Score.model_validate(entry))
        except ValidationError:
            log.warning("discarding malformed score entry: %r", entry)
    return scores


async def score_batch(settings: Settings, profile: ProfileData, jobs: list[Any]) -> list[Score]:
    if not jobs:
        return []
    if not settings.openrouter_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Set it in the environment (.env), or run with --no-llm."
        )

    body: dict[str, Any] = {
        "model": settings.llm_model,
        "max_tokens": _MAX_TOKENS,
        "temperature": 0,
        # Not optional: hidden reasoning is billed as output, and left enabled a reasoning model
        # spends the whole budget thinking and returns empty content - batch lost and paid for.
        "reasoning": {"enabled": False},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": build_prompt(profile, jobs)},
        ],
    }
    if settings.llm_provider:
        body["provider"] = {
            "order": [settings.llm_provider],
            "allow_fallbacks": False,
            # The profile and advert text leave our infrastructure here; never route them to a
            # backend that may train on them (AGENTS.md > privacy policy).
            "data_collection": "deny",
        }

    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        response = await client.post(
            _ENDPOINT,
            json=body,
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "X-Title": "trouveur",
            },
        )
    response.raise_for_status()
    payload = response.json()

    choices = payload.get("choices") or []
    if not choices:
        log.warning("LLM response carried no choices: %s", str(payload)[:200])
        return []
    text = (choices[0].get("message") or {}).get("content") or ""
    if choices[0].get("finish_reason") == "length":
        log.warning("LLM response hit max_tokens; batch of %d may be partly unscored", len(jobs))
    return parse_response(text)
