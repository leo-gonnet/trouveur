"""The paid stage: scoring a shortlist against one user's profile, on that user's own key.

All scoring prompt text lives here. Do not scatter fragments across modules -- a prompt assembled
from three files cannot be reviewed, and a change to one fragment silently rescores the corpus.

Cost discipline is structural rather than advisory:

  - Retrieval and the rules cut run first, so this only ever sees a shortlist.
  - Every result is cached by (content_hash, user, profile_version), so a posting is scored once
    per profile, ever. Re-scoring an unchanged posting is a bug, not an inefficiency.
  - The monthly ceiling is checked before each batch is sent, not reported after it returns. A
    retry loop on someone else's card is not something to find out about from the user.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from trouveur.config import Settings
from trouveur.match import llm
from trouveur.models import UserProfile

log = logging.getLogger(__name__)

BATCH_SIZE = 10
_DESCRIPTION_CHARS = 1500
# Output is billed as generated, not as budgeted, so headroom is free and buys immunity to a
# truncated response losing an entire batch.
_MAX_TOKENS = 3000

_SYSTEM = """You screen job adverts for one candidate. You are strict and concise.

Score each job 0-100 for how well it matches the candidate's profile and objectives:
  90-100  excellent fit, the candidate should apply today
  70-89   good fit, clearly worth reading
  40-69   plausible but compromised on an important dimension
  0-39    poor fit

Judge the substance of the role, not the polish of the advert. German and English adverts are
equally valid and neither is preferred. Penalise heavily: staffing agencies, disguised sales
roles, roles far junior or far senior to the candidate, and adverts that violate a stated
deal-breaker.

Return ONLY a JSON array, one object per job, no prose:
[{"id": <int>, "score": <int 0-100>, "reason": "<one sentence, max 25 words>",
  "red_flags": ["<short phrase>", ...]}]"""


class _Score(BaseModel):
    id: int
    score: int = Field(ge=0, le=100)
    reason: str = ""
    red_flags: list[str] = Field(default_factory=list)


@dataclass
class RerankReport:
    scored: int = 0
    from_cache: int = 0
    cost_usd: Decimal = Decimal(0)
    tokens_in: int = 0
    tokens_out: int = 0
    stopped_on_budget: bool = False
    errors: list[str] = field(default_factory=list)


def build_prompt(profile: UserProfile, candidates: list) -> str:
    listing = []
    for row in candidates:
        locations = ", ".join(
            item.get("raw", "") for item in (getattr(row, "locations", None) or [])
        )
        salary = (
            f"{int(row.salary_min_eur_year or 0)}-{int(row.salary_max_eur_year or 0)} EUR/year"
            if (row.salary_min_eur_year or row.salary_max_eur_year)
            else "unstated"
        )
        listing.append(
            f"### id={row.job_id}\n"
            f"title: {row.title}\n"
            f"company: {row.company or 'unknown'}\n"
            f"location: {locations or 'unknown'} | work mode: {row.work_mode or 'unknown'} | "
            f"salary: {salary}\n"
            f"description: {(row.description or '')[:_DESCRIPTION_CHARS]}"
        )

    return (
        "CANDIDATE PROFILE\n"
        f"current title: {profile.title or 'unstated'}\n"
        f"years of experience: {profile.years_experience}\n"
        f"languages: {', '.join(profile.languages) or 'unstated'}\n"
        f"objectives: {profile.objectives or 'unstated'}\n"
        f"must have: {'; '.join(profile.must_have) or 'none stated'}\n"
        f"deal breakers: {'; '.join(profile.deal_breakers) or 'none stated'}\n"
        f"minimum salary: {int(profile.min_salary_eur_year)} EUR/year\n\n"
        f"JOBS TO SCORE ({len(candidates)})\n\n" + "\n\n".join(listing)
    )


def parse_response(text: str) -> list[_Score]:
    """Parse the model's array. A malformed response yields nothing -- never a score of zero.

    Treating unparseable output as 0 would mark good jobs as bad and cache that verdict, and the
    user would never see them again.
    """
    cleaned = text.strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        log.warning("rerank response contained no JSON array; batch left unscored")
        return []
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        log.warning("rerank response was not valid JSON; batch left unscored")
        return []

    scores = []
    for entry in payload if isinstance(payload, list) else []:
        try:
            scores.append(_Score.model_validate(entry))
        except ValidationError:
            log.warning("discarding malformed score entry")
    return scores


def would_exceed_budget(
    spent: Decimal, budget: Decimal, estimate: Decimal
) -> bool:
    """Whether sending one more batch would cross the ceiling.

    Checked with an estimate from batches already sent this run, so the cap is enforced before the
    money is spent. Without an estimate the ceiling could only be noticed after being passed.
    """
    if budget <= 0:
        return True
    return spent + estimate > budget


async def score_batch(
    settings: Settings,
    profile: UserProfile,
    candidates: list,
    *,
    api_key: str,
    model: str,
    provider_pin: str | None,
) -> tuple[list[_Score], llm.Usage]:
    completion = await llm.complete(
        api_key=api_key,
        model=model,
        provider_pin=provider_pin,
        system=_SYSTEM,
        user=build_prompt(profile, candidates),
        max_tokens=_MAX_TOKENS,
        timeout=settings.llm_timeout_seconds,
    )
    if completion.truncated:
        log.warning(
            "rerank response hit max_tokens; %d jobs may be partly unscored", len(candidates)
        )
    return parse_response(completion.text), completion.usage
