"""The Quality page, per night: what the scorer said, and whether search found the kept jobs."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from trouveur import clock

DAYS = 30
# The score pill's "high" band by default, so "good" is what the reader sees as good.
GOOD = 85
# Dismissals arrive days after the score, and one night holds a handful of good jobs: a week
# smooths both without hiding a change for long.
AGREEMENT_NIGHTS = 7
# 90% Wilson interval. A share of a handful of jobs is mostly noise -- one dismissed job out of
# one reads as 100% -- so the share is drawn with the range it could really be in, and anything
# that alerts on it must read the lower bound.
_Z = 1.645
KEPT = {"saved", "applied"}


@dataclass(frozen=True)
class ScoreNight:
    day: date
    scored: int | None = None
    good: int | None = None
    # Good jobs out of scored ones: near 1 every night means the score no longer separates.
    share_good: float | None = None
    # Of the good jobs of the last AGREEMENT_NIGHTS nights, how many the reader acted on (kept
    # or dismissed) and how many of those they dismissed. Counts, so readers can be pooled.
    good_acted: int = 0
    good_dismissed: int = 0

    @property
    def dismissed_good(self) -> float | None:
        return self.good_dismissed / self.good_acted if self.good_acted else None

    @property
    def dismissed_range(self) -> tuple[float, float] | None:
        return wilson(self.good_dismissed, self.good_acted)


@dataclass(frozen=True)
class Night:
    day: date
    # None when no check ran that night: a gap, not a zero.
    needles: int | None = None
    found: int = 0
    added: list[int] = field(default_factory=list)
    # (job id, why it left the set): dismissed or unmarked.
    removed: list[tuple[int, str]] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        return self.found / self.needles if self.needles else None


def days(now: datetime, count: int = DAYS) -> list[date]:
    today = clock.to_local(now).date()
    return [today - timedelta(days=offset) for offset in range(count - 1, -1, -1)]


def score_nights(rows, now: datetime, good_from: int = GOOD) -> list[ScoreNight]:
    """One reader's nights. A night nothing was scored on is a gap, not a zero."""
    window = days(now)
    by_day: dict[date, list] = defaultdict(list)
    for row in rows:
        by_day[clock.to_local(row.scored_at).date()].append(row)
    nights = []
    for index, day in enumerate(window):
        scored = by_day.get(day, [])
        if not scored:
            nights.append(ScoreNight(day=day))
            continue
        good = [row for row in scored if row.llm_score >= good_from]
        recent_good = [
            row
            for earlier in window[max(0, index - AGREEMENT_NIGHTS + 1): index + 1]
            for row in by_day.get(earlier, [])
            if row.llm_score >= good_from
        ]
        acted = [row for row in recent_good if row.state in KEPT or row.state == "dismissed"]
        nights.append(
            ScoreNight(
                day=day,
                scored=len(scored),
                good=len(good),
                share_good=len(good) / len(scored),
                good_acted=len(acted),
                good_dismissed=sum(row.state == "dismissed" for row in acted),
            )
        )
    return nights


def wilson(hits: int, total: int) -> tuple[float, float] | None:
    """The range a share of `hits` out of `total` is likely to really be in."""
    if not total:
        return None
    share = hits / total
    centre = share + _Z * _Z / (2 * total)
    spread = _Z * math.sqrt(share * (1 - share) / total + _Z * _Z / (4 * total * total))
    scale = 1 + _Z * _Z / total
    return max(0.0, (centre - spread) / scale), min(1.0, (centre + spread) / scale)


def median_nights(per_reader: list[list[ScoreNight]]) -> list[ScoreNight]:
    """Night by night, the median reader on the counts and the share good; the dismissals are
    pooled instead, since a share of a few jobs per reader is only worth reading summed."""
    if len(per_reader) == 1:
        return per_reader[0]

    def median(values):
        values = [value for value in values if value is not None]
        return statistics.median(values) if values else None

    return [
        ScoreNight(
            day=nights[0].day,
            scored=median(night.scored for night in nights),
            good=median(night.good for night in nights),
            share_good=median(night.share_good for night in nights),
            good_acted=sum(night.good_acted for night in nights),
            good_dismissed=sum(night.good_dismissed for night in nights),
        )
        for nights in zip(*per_reader, strict=True)
    ]


def _why_gone(job) -> str:
    """Why a job stopped being a needle. Only the reader removes one: age and closing do not."""
    return "dismissed" if job is not None and job.state == "dismissed" else "unmarked"


def recall_nights(rows, jobs: dict, now: datetime) -> list[Night]:
    """One reader's nights. When a day holds several checks the last one counts; each night's
    changes are against the night before that had a check."""
    found_by_check: dict[int, dict[int, int | None]] = defaultdict(dict)
    when: dict[int, datetime] = {}
    for row in rows:
        when[row.check_id] = row.checked_at
        if row.job_id is not None:
            found_by_check[row.check_id][row.job_id] = row.rank
    latest_per_day: dict[date, int] = {}
    for check_id, at in sorted(when.items(), key=lambda item: item[1]):
        latest_per_day[clock.to_local(at).date()] = check_id

    nights = []
    before: set[int] | None = None
    for day in days(now):
        check_id = latest_per_day.get(day)
        if check_id is None:
            nights.append(Night(day=day))
            continue
        ranks = found_by_check.get(check_id, {})
        now_set = set(ranks)
        nights.append(
            Night(
                day=day,
                needles=len(ranks),
                found=sum(rank is not None for rank in ranks.values()),
                added=sorted(now_set - before) if before is not None else [],
                removed=(
                    [(job, _why_gone(jobs.get(job))) for job in sorted(before - now_set)]
                    if before is not None else []
                ),
                missing=sorted(job for job, rank in ranks.items() if rank is None),
            )
        )
        before = now_set
    return nights


def pooled(per_reader: list[list[Night]]) -> list[Night]:
    """All readers' needles together: recall is a share of jobs, so it pools rather than takes
    a median, and a reader with one needle does not weigh like one with twenty."""
    if len(per_reader) == 1:
        return per_reader[0]
    pooled_nights = []
    for nights in zip(*per_reader, strict=True):
        checked = [night for night in nights if night.needles is not None]
        pooled_nights.append(
            Night(
                day=nights[0].day,
                needles=sum(night.needles for night in checked) if checked else None,
                found=sum(night.found for night in checked),
                added=[job for night in checked for job in night.added],
                removed=[change for night in checked for change in night.removed],
                missing=[job for night in checked for job in night.missing],
            )
        )
    return pooled_nights


def last_week(nights: list[Night]) -> tuple[int, int]:
    """Found and needles over the last seven nights, for the headline."""
    recent = [night for night in nights[-7:] if night.needles]
    return sum(night.found for night in recent), sum(night.needles for night in recent)
