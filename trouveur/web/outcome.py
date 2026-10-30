"""What a run produced, read back from the report it stored, for the Runs table."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import median

from trouveur import clock

USUAL_NIGHTS = 7


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


def nightly(rows, days: list[date], value) -> list:
    """Per night, `value` of the night's last row, None where there is none. Rows come oldest
    first, and a row belongs to the night it started, as on the runs chart."""
    by_day = {clock.to_local(row.started_at).date(): value(row) for row in rows}
    return [by_day.get(day) for day in days]


def usual_and_last(totals: list[int | None]) -> tuple[int | None, int | None]:
    """The median of the USUAL_NIGHTS counted nights before the last one, and the last one."""
    counted = [total for total in totals if total is not None]
    if not counted:
        return None, None
    before = counted[-USUAL_NIGHTS - 1 : -1]
    return (int(median(before)) if before else None), counted[-1]
