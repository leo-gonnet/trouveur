"""The line charts' geometry: gaps, the trimmed window and the y axis."""

from __future__ import annotations

from datetime import date, timedelta

from trouveur.web import linechart

DAYS = [date(2026, 10, 1) + timedelta(days=n) for n in range(30)]


def test_a_night_without_a_value_breaks_the_line_instead_of_dropping_to_zero():
    values = [3.0] * 10 + [None] * 3 + [4.0] * 17
    series = linechart.build(DAYS, [("kept", "kept", values)]).series[0]
    assert len(series.lines) == 2
    assert series.points[10] is None and series.last == 4.0


def test_the_window_starts_at_the_first_night_with_data_but_keeps_a_week():
    late = [None] * 25 + [1.0] * 5
    assert linechart.trim(DAYS, late) == 30 - linechart.MIN_NIGHTS
    early = [None] * 10 + [1.0] * 20
    assert linechart.trim(DAYS, early, late) == 10


def test_a_count_axis_tops_at_a_round_number_and_skips_a_fractional_middle():
    chart = linechart.build(DAYS, [("kept", "kept", [45.0] * 30)])
    assert [label for label, _ in chart.y_ticks] == ["0", "45"], "a middle of 22.5 jobs is left out"
    chart = linechart.build(DAYS, [("kept", "kept", [58.0] * 30)])
    assert [label for label, _ in chart.y_ticks] == ["0", "30", "60"]


def test_a_percent_axis_runs_to_one_hundred():
    chart = linechart.build(DAYS, [("share", "share", [0.2] * 30)], percent=True)
    assert [label for label, _ in chart.y_ticks] == ["0%", "50%", "100%"]
