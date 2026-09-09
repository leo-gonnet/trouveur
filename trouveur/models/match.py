"""Per-user match state: what the pipeline decided about a job *for one user*.

Nothing here belongs on the job row. The corpus is shared and user-agnostic; verdicts, scores,
notifications and saved/dismissed state are per user, and V1's habit of hanging them off the job
is what made a second user impossible to add.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class UserState(StrEnum):
    NEW = "new"
    SAVED = "saved"
    APPLIED = "applied"
    DISMISSED = "dismissed"


class RuleVerdict(StrEnum):
    PASS = "pass"
    REJECT = "reject"
    UNKNOWN = "unknown"


class Candidate(BaseModel):
    """A job that survived retrieval, on its way to the deterministic cut and the reranker."""

    job_id: int
    content_hash: bytes
    title: str
    company: str | None = None
    description: str | None = None
    location: str | None = None
    salary_min_eur_year: int | None = None
    salary_max_eur_year: int | None = None
    work_mode: str | None = None
    # Derived from the source's own structured flag, not from prose. None means the posting has
    # no detail yet, which is genuinely unknown rather than "not an agency".
    is_agency: bool | None = None
    retrieval_score: float = 0.0
    dense_rank: int | None = None
    lexical_rank: int | None = None


class RerankResult(BaseModel):
    job_id: int
    score: int = Field(ge=0, le=100)
    reason: str = ""
    red_flags: list[str] = Field(default_factory=list)
