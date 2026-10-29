"""Where each run's time went: step durations, and bars on a time-of-day axis for the Runs page."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from trouveur import clock
from trouveur.models import RunStage

STEPS = (RunStage.FETCH, RunStage.PREPARE, RunStage.MATCH, RunStage.EMAIL, RunStage.DISCOVER)


@dataclass(frozen=True)
class Segment:
    # None for a run from before steps were recorded: its whole length, unsplit.
    stage: str | None
    left: float
    width: float
    start: datetime
    end: datetime
    running: bool

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class Bar:
    run_id: int
    day: str
    segments: list[Segment]


@dataclass(frozen=True)
class Timeline:
    bars: list[Bar]
    # (label, percent from the left) for each hour mark.
    ticks: list[tuple[str, float]]

    @property
    def has_unsplit(self) -> bool:
        return any(seg.stage is None for bar in self.bars for seg in bar.segments)


def step_spans(run, now: datetime) -> list[tuple[str | None, datetime, datetime]]:
    """Each step's start and end. A step ends where the next begins, the last at the run's end.

    The first step starts with the run: whatever the runner did before it named a step belongs
    to that step rather than to a gap. Empty for a run that never started.
    """
    if run.started_at is None:
        return []
    end = run.finished_at or now
    order = [step.value for step in STEPS]
    began = sorted(
        ((datetime.fromisoformat(value), stage)
         for stage, value in (run.stage_started or {}).items() if stage in order),
        key=lambda item: (item[0], order.index(item[1])),
    )
    if not began:
        return [(None, run.started_at, end)]
    starts = [run.started_at] + [at for at, _ in began[1:]]
    ends = starts[1:] + [end]
    return [
        (stage, start, stop)
        for (_, stage), start, stop in zip(began, starts, ends, strict=True)
    ]


def step_seconds(run, now: datetime) -> dict[str, float]:
    return {
        stage: (stop - start).total_seconds()
        for stage, start, stop in step_spans(run, now)
        if stage is not None
    }


def build(runs, now: datetime) -> Timeline | None:
    """Full runs only, newest on top, placed by local time of day so days line up.

    The axis starts and ends on the quarter hour around the bars, so the space left of the
    earliest start is at most fifteen minutes; the hour marks fall inside it.

    A scoring-only run is left out: it starts whenever a reader saves a profile, and one at
    three in the afternoon would stretch the axis until the nightly runs are slivers.
    """
    placed = []
    for run in runs:
        if run.match_user_id is not None:
            continue
        spans = step_spans(run, now)
        if not spans:
            continue
        start = clock.to_local(run.started_at)
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        offset = (start - midnight).total_seconds() / 60
        minutes = [
            (stage, offset + (a - run.started_at).total_seconds() / 60,
             offset + (b - run.started_at).total_seconds() / 60)
            for stage, a, b in spans
        ]
        placed.append((run, start, minutes))
    if not placed:
        return None

    low = math.floor(min(m[0][1] for *_, m in placed) / 15) * 15
    high = max(math.ceil(max(m[-1][2] for *_, m in placed) / 15) * 15, low + 60)
    span = high - low

    def pct(minute: float) -> float:
        return round(100 * (minute - low) / span, 2)

    today = clock.to_local(now).date()
    names = {today: "today", today - timedelta(days=1): "yesterday"}
    bars = [
        Bar(
            run_id=run.id,
            day=names.get(start.date(), start.strftime("%a %d")),
            segments=[
                Segment(
                    stage=stage, left=pct(a), width=round(pct(b) - pct(a), 2),
                    start=start + timedelta(minutes=a - minutes[0][1]),
                    end=start + timedelta(minutes=b - minutes[0][1]),
                    running=run.finished_at is None and index == len(minutes) - 1,
                )
                for index, (stage, a, b) in enumerate(minutes)
            ],
        )
        for run, start, minutes in placed
    ]
    step = 60 if span <= 8 * 60 else 120
    # A mark on the right edge would hang half outside the card.
    hours = range(math.ceil(low / step) * step, high, step)
    ticks = [(f"{minute // 60 % 24:02d}:00", pct(minute)) for minute in hours]
    return Timeline(bars=bars, ticks=ticks)
