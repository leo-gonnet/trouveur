"""Per-user match state: what the pipeline decided about a job *for one user*.

Nothing here belongs on the job row. The corpus is shared and user-agnostic; scores,
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


class RerankResult(BaseModel):
    job_id: int
    score: int = Field(ge=0, le=100)
    reason: str = ""
    red_flags: list[str] = Field(default_factory=list)
