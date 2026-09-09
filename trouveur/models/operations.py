"""Operational vocabulary: the states of a run and the provenance of a tenant.

These exist for the same reason the domain enums do. Written as bare strings, `status="runing"`
type-checks, lints and reaches Postgres before anything notices, and the failure lands on whichever
run happens to be executing rather than on the commit that caused it.
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class RunTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class TenantOrigin(StrEnum):
    MANUAL = "manual"
    # Proposed by a discovery pass. Always registered disabled: discovery proposes, a human
    # promotes, so it can never enlarge the crawl or the bill on its own.
    DISCOVERED = "discovered"
