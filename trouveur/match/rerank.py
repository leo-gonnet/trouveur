"""The paid stage: scoring a shortlist against one user's profile, on that user's own key.

ALL scoring prompt text lives here; a prompt assembled from three files cannot be reviewed.
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

# How many postings the model reads per run, best retrieval score first, and so also how long the
# Recommendations page is. A constant rather than a per-user setting: it was one number doing
# three jobs -- list length, bill and pause -- and the bill is already capped, in dollars, by the
# user's monthly ceiling. What is left, stopping spending, is now an explicit switch.
RERANK_LIMIT = 150
_DESCRIPTION_CHARS = 1500
# Billed as generated, not as budgeted, so headroom is free.
_MAX_TOKENS = 3000

_SYSTEM = """You screen job adverts for one candidate. You are strict and concise.

Score each job 0-100 for how well it matches the candidate's profile and objectives:
  90-100  excellent fit, the candidate should apply today
  70-89   good fit, clearly worth reading
  40-69   plausible but compromised on an important dimension
  0-39    poor fit

Where a background is given, it is evidence of what the candidate can already do and the
objectives are what they want next. Weigh both: a role the background qualifies them for but the
objectives reject is a poor fit, and so is one the objectives ask for but nothing in the
background supports.

Judge the substance of the role, not the polish of the advert. German and English adverts are
equally valid and neither is preferred. Penalise heavily: staffing agencies, disguised sales
roles, internships and working-student roles, and roles far junior or far senior to the
candidate. Preferred cities are a preference, not a requirement: a role there, nearby, or remote
satisfies it; elsewhere is a compromise, never a rejection.

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


def build_prompt(profile: UserProfile, candidates: list, *, background: str = "") -> str:
    """The profile block, then the batch.

    `background` is the distilled summary from the expansion stage and MUST stay an argument:
    reading `profile.background` here looks identical in a diff and bills a 4,000-character CV
    once per batch instead of once per profile version.
    """
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
        f"preferred cities: {', '.join(profile.cities) or 'none stated'}\n"
        f"objectives: {profile.objectives or 'unstated'}\n"
        f"background: {background or 'unstated'}\n"
        f"must have: {'; '.join(profile.must_have) or 'none stated'}\n"
        f"minimum salary: {int(profile.min_salary_eur_year)} EUR/year\n\n"
        f"JOBS TO SCORE ({len(candidates)})\n\n" + "\n\n".join(listing)
    )


def parse_response(text: str) -> list[_Score]:
    """Parse the model's array. A malformed response yields nothing -- NEVER a score of zero,
    which would cache a wrong verdict and hide a good job permanently."""
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
    """Whether sending one more batch would cross the ceiling, checked before the money is
    spent rather than reported after."""
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
    background: str = "",
) -> tuple[list[_Score], llm.Usage]:
    completion = await llm.complete(
        api_key=api_key,
        model=model,
        provider_pin=provider_pin,
        system=_SYSTEM,
        user=build_prompt(profile, candidates, background=background),
        max_tokens=_MAX_TOKENS,
        timeout=settings.llm_timeout_seconds,
    )
    if completion.truncated:
        log.warning(
            "rerank response hit max_tokens; %d jobs may be partly unscored", len(candidates)
        )
    return parse_response(completion.text), completion.usage
