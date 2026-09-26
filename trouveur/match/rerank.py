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

# ONE posting per call, and there is no batch size to raise. Ten in a prompt was measured to move
# scores by where a posting sat in it: slot 0 scored 8-15 points above slot 9, and the same posting
# beside nine weak ones rather than nine strong ones moved 8-39. Batches were filled in retrieval
# order, so neither effect washed out. Scoring alone measured +0.07 to +0.09 nDCG@20 against
# evalx/reference.json. Evidence: evalx/FINDINGS-RERANKER.md.
#
# Nothing caps how many postings a run scores any more. The old RERANK_LIMIT of 150 existed to keep
# the bill small, which the monthly ceiling already does in dollars, and as a side effect it decided
# how long the Recommendations page was -- so a user who changed their profile met their corpus 150
# a day for a fortnight.

# Scoring walks the retrieval order in blocks and stops once the scores run out, which is what
# decides how long an edition is. Nothing else does: a rank cut is arbitrary, and a score
# THRESHOLD hides postings the reader already paid for behind a number they have to guess. This
# hides nothing -- everything scored is published; the rule only decides when to stop buying.
#
# Two consecutive weak blocks rather than one, because retrieval order correlates only loosely
# with the model's verdict and a single weak block is noise. A dead day therefore costs 50 calls
# and stops; a rich day keeps buying for as long as it keeps finding.
BLOCK = 25
SCORE_FLOOR = 30
WEAK_BLOCKS_BEFORE_STOPPING = 2

# How many of those calls are in flight at once. Wall clock, not money, is what a run of a few
# thousand postings is bounded by -- sequentially, at roughly two seconds a call, it would take
# over an hour. Kept modest because these go to one user's own OpenRouter key.
CONCURRENCY = 8

# Enough of an advert to hold the requirements. 1,500 characters is roughly 375 tokens, which on a
# German advert is the greeting and the company boilerplate and none of what the role actually asks
# for.
_DESCRIPTION_CHARS = 6000
# One score object out. Billed as generated, not as budgeted, so the headroom above it is free.
_MAX_TOKENS = 400

_SYSTEM = """You screen job adverts for one candidate. You are strict and concise.

Score the advert 0-100 for how well it matches the candidate's profile and objectives:
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

Return ONLY a JSON object, no prose:
{"score": <int 0-100>, "reason": "<one sentence, max 25 words>",
 "red_flags": ["<short phrase>", ...]}"""


class _Score(BaseModel):
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


def build_prompt(profile: UserProfile, candidate, *, background: str = "") -> str:
    """The profile block, then the one advert being scored.

    That order is load-bearing twice over. The profile block and the system prompt are byte for byte
    identical across every call of a run, so a provider that caches prompt prefixes gets the whole
    of it for free; putting the advert first would make every call a fresh prefix. And the advert
    being last is what keeps the model's attention on the thing it is scoring.

    `background` is the distilled summary from the expansion stage and MUST stay an argument:
    reading `profile.background` here looks identical in a diff and bills a 4,000-character CV once
    per posting instead of once per profile version.
    """
    locations = ", ".join(
        item.get("raw", "") for item in (getattr(candidate, "locations", None) or [])
    )
    salary = (
        f"{int(candidate.salary_min_eur_year or 0)}-{int(candidate.salary_max_eur_year or 0)}"
        " EUR/year"
        if (candidate.salary_min_eur_year or candidate.salary_max_eur_year)
        else "unstated"
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
        "JOB ADVERT\n"
        f"title: {candidate.title}\n"
        f"company: {candidate.company or 'unknown'}\n"
        f"location: {locations or 'unknown'} | work mode: {candidate.work_mode or 'unknown'} | "
        f"salary: {salary}\n"
        f"description: {(candidate.description or '')[:_DESCRIPTION_CHARS]}"
    )


def parse_score(text: str) -> _Score | None:
    """Parse the model's object. A malformed response yields None -- NEVER a score of zero, which
    would cache a wrong verdict and hide a good job permanently."""
    cleaned = text.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        log.warning("rerank response contained no JSON object; posting left unscored")
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        log.warning("rerank response was not valid JSON; posting left unscored")
        return None
    try:
        return _Score.model_validate(payload)
    except ValidationError:
        log.warning("discarding malformed score object; posting left unscored")
        return None


def would_exceed_budget(
    spent: Decimal, budget: Decimal, estimate: Decimal
) -> bool:
    """Whether sending one more batch would cross the ceiling, checked before the money is
    spent rather than reported after."""
    if budget <= 0:
        return True
    return spent + estimate > budget


async def score_one(
    settings: Settings,
    profile: UserProfile,
    candidate,
    *,
    api_key: str,
    model: str,
    provider_pin: str | None,
    background: str = "",
) -> tuple[_Score | None, llm.Usage]:
    """Score one posting. A response this cannot parse leaves it unscored, so the next run tries
    again -- it is never cached, and never recorded as a zero."""
    completion = await llm.complete(
        api_key=api_key,
        model=model,
        provider_pin=provider_pin,
        system=_SYSTEM,
        user=build_prompt(profile, candidate, background=background),
        max_tokens=_MAX_TOKENS,
        timeout=settings.llm_timeout_seconds,
    )
    if completion.truncated:
        log.warning("rerank response hit max_tokens; posting %s left unscored", candidate.job_id)
    return parse_score(completion.text), completion.usage
