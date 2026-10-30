"""Line charts drawn as inline SVG: one value per night per series, on one shared y axis.

Geometry only; the template draws it and app.css colours it. A night without a value breaks the
line rather than dropping it to zero, so a night with no data never reads as a bad night.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

WIDTH = 600
HEIGHT = 180
LEFT = 40
RIGHT = 8
TOP = 10
BOTTOM = 22
# Fewer nights than this and the chart is a few dots: keep a week on screen at least.
MIN_NIGHTS = 7


@dataclass(frozen=True)
class Series:
    key: str
    label: str
    # One polyline per unbroken run of nights, as an SVG `points` string.
    lines: list[str]
    # The same runs closed down to the baseline, for a series drawn as an area.
    areas: list[str]
    # One entry per night, None where the series has no value.
    points: list[tuple[float, float] | None]
    last: float | None


@dataclass(frozen=True)
class Chart:
    series: list[Series]
    # Shaded ranges, as closed SVG polygons, one per unbroken run of nights.
    ranges: list[str]
    y_ticks: list[tuple[str, float]]
    x_ticks: list[tuple[str, float]]
    # One hover band per night: (left x, width, night).
    bands: list[tuple[float, float, date]]
    width: int
    height: int
    left: int
    right: int
    bottom: int


def trim(days: list[date], *values: list) -> int:
    """Index of the first night any series has a value, keeping at least MIN_NIGHTS on screen:
    thirty columns of nothing before the data is what pushed every chart to the right."""
    first = next(
        (index for index in range(len(days)) if any(v[index] is not None for v in values)),
        len(days),
    )
    return max(0, min(first, len(days) - MIN_NIGHTS))


def _nice_top(value: float) -> float:
    """The next half step above the value: 58 tops at 60, 4 at 4, 0.3 at 0.5."""
    if value <= 0:
        return 1
    half = 10 ** math.floor(math.log10(value)) / 2
    return math.ceil(value / half) * half


def build(
    days: list[date],
    series: list[tuple[str, str, list[float | None]]],
    *,
    ranges: list[tuple[float, float] | None] = (),
    percent: bool = False,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> Chart:
    """`series` is (key, label, one value per night). Percent charts run 0 to 100%.

    `width` is in the SVG's own units and should be close to the pixels it gets on screen: the
    chart scales to its box, and its text scales with it.
    """
    count = len(days)
    plot_w = width - LEFT - RIGHT
    plot_h = height - TOP - BOTTOM
    top = 1.0 if percent else _nice_top(
        max((v for _, _, values in series for v in values if v is not None), default=0)
    )

    def x(index: int) -> float:
        return round(LEFT + plot_w * (index + 0.5) / count, 1)

    def y(value: float) -> float:
        return round(TOP + plot_h * (1 - value / top), 1)

    built = []
    for key, label, values in series:
        runs: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for index, value in enumerate(values):
            if value is None:
                if current:
                    runs.append(current)
                current = []
                continue
            current.append((x(index), y(value)))
        if current:
            runs.append(current)
        baseline = TOP + plot_h
        built.append(
            Series(
                key=key,
                label=label,
                lines=[" ".join(f"{px},{py}" for px, py in run) for run in runs],
                areas=[
                    f"{run[0][0]},{baseline} "
                    + " ".join(f"{px},{py}" for px, py in run)
                    + f" {run[-1][0]},{baseline}"
                    for run in runs
                ],
                points=[
                    (x(index), y(value)) if value is not None else None
                    for index, value in enumerate(values)
                ],
                last=next((v for v in reversed(values) if v is not None), None),
            )
        )

    shaded = []
    run: list[tuple[int, float, float]] = []
    for index, span in enumerate([*ranges, None]):
        if span is None:
            if run:
                upper = " ".join(f"{x(i)},{y(high)}" for i, _, high in run)
                lower = " ".join(f"{x(i)},{y(low)}" for i, low, _ in reversed(run))
                shaded.append(f"{upper} {lower}")
            run = []
            continue
        run.append((index, *span))

    def label(value: float) -> str:
        return f"{round(100 * value)}%" if percent else f"{value:,g}"

    every = max(1, round(count / 5))
    x_ticks = [
        ("today" if index == count - 1 else day.strftime("%d %b"), x(index))
        for index, day in enumerate(days)
        if index == count - 1 or (index % every == 0 and count - 1 - index >= every / 2)
    ]
    band = plot_w / count
    return Chart(
        series=built,
        ranges=shaded,
        # A count's middle mark only when it is a whole number: "22.5 jobs" reads as a bug.
        y_ticks=[
            (label(top * f), y(top * f))
            for f in (0, 0.5, 1)
            if percent or float(top * f).is_integer()
        ],
        x_ticks=x_ticks,
        width=width,
        height=height,
        left=LEFT,
        right=width - RIGHT,
        bottom=height - BOTTOM,
        bands=[
            (round(LEFT + band * index, 1), round(band, 1), day) for index, day in enumerate(days)
        ],
    )


SPARK_WIDTH = 96
SPARK_HEIGHT = 20
SPARK_PAD = 2


@dataclass(frozen=True)
class Spark:
    lines: list[str]
    last: tuple[float, float] | None
    top: float


def spark(values: list[float | None]) -> Spark:
    """A table-cell line with no axes, from zero to the highest night."""
    count = len(values)
    top = max((v for v in values if v is not None), default=0) or 1
    plot_w = SPARK_WIDTH - 2 * SPARK_PAD
    plot_h = SPARK_HEIGHT - 2 * SPARK_PAD

    def point(index: int, value: float) -> tuple[float, float]:
        x = SPARK_PAD + (plot_w * index / (count - 1) if count > 1 else plot_w / 2)
        return round(x, 1), round(SPARK_PAD + plot_h * (1 - value / top), 1)

    runs: list[list[tuple[float, float]]] = [[]]
    for index, value in enumerate(values):
        if value is None:
            runs.append([])
        else:
            runs[-1].append(point(index, value))
    last = next(
        (point(i, v) for i, v in reversed(list(enumerate(values))) if v is not None), None
    )
    return Spark(
        lines=[" ".join(f"{x},{y}" for x, y in run) for run in runs if len(run) > 1],
        last=last,
        top=top,
    )
