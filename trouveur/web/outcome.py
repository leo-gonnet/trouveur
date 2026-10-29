"""What a run produced, read back from the report it stored, for the Runs table."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from trouveur import clock


@dataclass(frozen=True)
class Outcome:
    # None where the report has no such part: a scoring-only run fetches nothing, and runs from
    # before a field was recorded do not have it.
    changed: int | None = None
    closed: int | None = None
    scored: int | None = None
    recommended: int | None = None
    cost_usd: Decimal | None = None
    emails: int | None = None
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def of(report: dict | None) -> Outcome:
    if not report:
        return Outcome()
    sources = report.get("sources")
    matches = report.get("matches")
    return Outcome(
        changed=_total(sources, "changed"),
        closed=_total(sources, "closed"),
        scored=_total(matches, "scored"),
        recommended=_total(matches, "published"),
        cost_usd=(
            sum((Decimal(row.get("cost_usd") or 0) for row in matches), Decimal(0))
            if matches else None
        ),
        emails=report.get("digests_sent"),
        failed=sorted(
            name for name, row in (sources or {}).items()
            if row.get("error") and not row.get("blocked")
        ),
        blocked=sorted(name for name, row in (sources or {}).items() if row.get("blocked")),
    )


def _total(rows, key: str) -> int | None:
    if not rows:
        return None
    values = rows.values() if isinstance(rows, dict) else rows
    if not any(key in row for row in values):
        return None
    return sum(int(row.get(key) or 0) for row in values)


def unknown_location_nights(rows, days: list[date]) -> list[dict[str, int] | None]:
    """Per night, the count of the night's last run, by source. None where no run counted."""
    by_day = {clock.to_local(row.finished_at).date(): row.unknown_location for row in rows}
    return [by_day.get(day) for day in days]
