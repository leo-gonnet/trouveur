"""Operational vocabulary: the states of a run and the provenance of a tenant."""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class TenantOrigin(StrEnum):
    MANUAL = "manual"
    # Always registered disabled: discovery proposes, a human promotes.
    DISCOVERED = "discovered"
